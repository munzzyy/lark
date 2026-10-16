"""Overlay server and Twitch chat, running together on one asyncio loop in a QThread."""
import asyncio
import errno
import logging
import time

from PySide6.QtCore import QObject, QThread, Signal

from .alerts import AlertEngine
from .events import sample_event
from .eventsub import EventSubClient
from .overlay_server import ChatHub, OverlayServer
from .keyring import SecretTool
from .twitch_api import TwitchClient, TwitchError, blank_state
from .twitch_irc import TwitchChat

log = logging.getLogger("lark.services")


class _Loop(QThread):
    def __init__(self, svc):
        super().__init__()
        self.svc = svc
        self.loop = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.svc._main())
        except Exception:
            log.exception("services loop crashed")
            self.svc.overlay_state.emit("error", "services crashed, see the log")
        finally:
            self.loop.close()


class Services(QObject):
    chat_message = Signal(object)
    chat_state = Signal(str, str)
    overlay_state = Signal(str, str)
    twitch_changed = Signal(object)
    event_added = Signal(object)
    eventsub_state = Signal(str, str)
    _done = Signal(object, bool, object)

    def __init__(self, config, parent=None, twitch_kw=None, irc_kw=None, eventsub_kw=None, log_dir=None):
        """The *_kw arguments exist so tests can point at local fakes."""
        super().__init__(parent)
        self.config = config
        self.hub = ChatHub()
        self.server = OverlayServer(self.hub, lambda: config.values)
        self.twitch_state = blank_state()
        tkw = {"store": SecretTool(), **(twitch_kw or {})}
        self.twitch = TwitchClient(lambda: config.get("twitch_client_id"), tkw.pop("store"), self._twitch_update, **tkw)
        self.chat = TwitchChat(lambda: config.get("twitch_channel"), self._on_message,
                               lambda s, d: self.chat_state.emit(s, d), auth=self.twitch.chat_auth,
                               **(irc_kw or {}))
        self.alerts = AlertEngine(lambda: config.values, self.event_added.emit, log_dir)
        self.alerts.load()
        self.server.extensions.append(self.alerts.register)
        # Tests that fake Twitch must not reach the real EventSub socket unless they also fake that.
        self.eventsub = EventSubClient(self.twitch, self.alerts.ingest, self.eventsub_state.emit, **(eventsub_kw or {})) \
            if eventsub_kw is not None or not twitch_kw else None
        self.eventsub_status = ("idle", "")
        self.eventsub_state.connect(lambda s, d: setattr(self, "eventsub_status", (s, d)))
        self._connect_future = None
        self._done.connect(self._finish)
        self._thread = _Loop(self)
        self._stopping = None
        self.port = None
        self._wanted_port = None
        self._ready = False

    def start(self):
        self._thread.start()

    async def _main(self):
        self._stopping = asyncio.Event()
        await self._start_server()
        chat_task = asyncio.ensure_future(self.chat.run())
        restore = asyncio.ensure_future(self.twitch.restore())
        sub_task = asyncio.ensure_future(self.eventsub.run()) if self.eventsub else None
        self._ready = True
        await self._stopping.wait()
        restore.cancel()
        self.twitch.cancel()
        if sub_task:
            await self.eventsub.stop()
            await asyncio.wait({sub_task}, timeout=5)
        await self.chat.stop()
        try:
            await asyncio.wait_for(chat_task, 2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # stop() only closes the socket; if that was missed (stop landed mid-handshake, or the box was busy)
            # the reader would sit there for minutes, so cancel it.
            chat_task.cancel()
            await asyncio.gather(chat_task, return_exceptions=True)
        await self.server.stop()

    async def _start_server(self):
        want = self.config.get("overlay_port")
        self._wanted_port = want
        try:
            self.port = await self.server.start(want)
            self.overlay_state.emit("running", f"http://127.0.0.1:{self.port}")
            return
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                self.port = None
                self.overlay_state.emit("error", f"Port {want} unavailable: {e.strerror}")
                return
        try:
            self.port = await self.server.start(0)
        except OSError as e:
            self.port = None
            self.overlay_state.emit("error", f"Port {want} unavailable: {e.strerror}")
            return
        self.overlay_state.emit("fallback", f"Port {want} is busy, overlays are on port {self.port} instead")

    def _twitch_update(self, state):
        could = self.twitch_state.get("status") == "connected"
        self.twitch_state = state
        self.twitch_changed.emit(state)
        if could != (state["status"] == "connected"):
            self.chat.reconnect_now(retry_auth=True)
        if self.eventsub:
            self.eventsub.kick()

    def _on_message(self, msg):
        self.hub.publish(msg)
        if "delete" not in msg:
            self.chat_message.emit(msg)

    def _call(self, coro_fn):
        loop = self._thread.loop
        if loop is None or loop.is_closed() or not self._ready:
            return
        asyncio.run_coroutine_threadsafe(coro_fn(), loop)

    def apply_settings(self):
        """Call after the overlay port or channel changed."""
        async def go():
            if self._wanted_port != self.config.get("overlay_port") or self.port is None:
                await self.server.stop()
                await self._start_server()
            self.chat.reconnect_now()
        self._call(go)

    def submit(self, coro_fn, cb=None):
        """Run an async function on the services loop. cb(ok, result_or_error_text) runs on the UI thread."""
        loop = self._thread.loop
        if loop is None or loop.is_closed() or not self._ready:
            if cb:
                cb(False, "Lark is still starting")
            return None

        async def go():
            try:
                return True, await coro_fn()
            except TwitchError as e:
                return False, str(e)
        coro = go()
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            if cb:
                cb(False, "Lark is stopping")
            return None
        fut.add_done_callback(lambda f: self._done.emit(cb, *self._result(f)))
        return fut

    @staticmethod
    def _result(f):
        try:
            return f.result()
        except asyncio.CancelledError:
            return False, "cancelled"
        except Exception as e:
            log.exception("service task failed")
            return False, str(e)

    def _finish(self, cb, ok, payload):
        if cb:
            try:
                cb(ok, payload)
            except Exception:
                log.exception("service callback failed")

    def twitch_connect(self, cb=None):
        self.twitch_cancel()
        self._connect_future = self.submit(self.twitch.connect, cb)

    def twitch_cancel(self):
        loop = self._thread.loop
        if loop and self._ready:
            loop.call_soon_threadsafe(self.twitch.cancel)

    def twitch_disconnect(self, cb=None):
        self.submit(self.twitch.disconnect, cb)

    def twitch_set_channel(self, title, game_id, cb):
        self.submit(lambda: self.twitch.set_channel(title, game_id), cb)

    def twitch_search(self, query, cb):
        self.submit(lambda: self.twitch.search_categories(query), cb)

    def twitch_refresh_channel(self, cb=None):
        self.submit(self.twitch.get_channel, cb)

    def send_chat(self, text, cb):
        async def go():
            if not await self.chat.send(text):
                raise TwitchError("Chat is not connected yet")
            return True
        self.submit(go, cb)

    def test_alert(self, kind):
        """Inject a sample event through the same path real ones take."""
        e = sample_event(kind)

        async def go():
            self.alerts.ingest(e)
        self._call(go)

    def set_starting(self, title, minutes):
        end = time.time() + minutes * 60 if minutes else None
        self.server.starting = {"title": title, "end": end}

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}" if self.port else None

    def stop(self):
        loop = self._thread.loop
        if loop and self._stopping:
            loop.call_soon_threadsafe(self._stopping.set)
        self._thread.wait(8000)
