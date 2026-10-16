"""Runs ObsClient on an asyncio loop inside a QThread and hands results to the UI thread."""
import asyncio
import logging
import math

from PySide6.QtCore import QObject, QThread, Signal

from .obsws import ObsClient, ObsError

log = logging.getLogger("lark.bridge")


def _db(x):
    return 20 * math.log10(x) if x > 1e-6 else -120.0


def parse_meters(data):
    """InputVolumeMeters payload -> {inputName: (level_db, peak_db)} using the loudest channel."""
    out = {}
    for inp in data.get("inputs", []):
        mag = peak = 0.0
        for ch in inp.get("inputLevelsMul", []):
            if len(ch) >= 2:
                mag = max(mag, float(ch[0]))
                peak = max(peak, float(ch[1]))
        out[inp.get("inputName", "")] = (_db(mag), _db(peak))
    return out


class _Loop(QThread):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.loop = None
        self.ready = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        b = self.bridge
        b.client = ObsClient(b.config.obs_connection, b._on_event, lambda s, d: b.state.emit(s, d))
        self.ready = True
        try:
            self.loop.run_until_complete(b.client.run())
        finally:
            self.loop.close()


class ObsBridge(QObject):
    state = Signal(str, str)
    event = Signal(str, object)
    meters = Signal(object)
    _done = Signal(object, bool, object)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.client = None
        self._thread = _Loop(self)
        self._done.connect(self._finish)

    def start(self):
        self._thread.start()

    def stop(self):
        t = self._thread
        if t.loop and self.client:
            fut = asyncio.run_coroutine_threadsafe(self.client.stop(), t.loop)
            try:
                fut.result(3)
            except Exception:
                log.exception("stopping the OBS client failed")
        t.wait(4000)

    def _on_event(self, etype, data):
        if etype == "InputVolumeMeters":
            self.meters.emit(parse_meters(data))
        else:
            self.event.emit(etype, data)

    def request(self, rtype, data=None, cb=None):
        """cb(ok, payload) runs on the UI thread. payload is the response dict or the error text."""
        loop = self._thread.loop
        if loop is None or self.client is None:
            if cb:
                cb(False, "not started")
            return

        async def go():
            try:
                return True, await self.client.request(rtype, data)
            except ObsError as e:
                return False, e.comment

        if loop.is_closed():
            if cb:
                cb(False, "OBS bridge stopped")
            return
        coro = go()
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            if cb:
                cb(False, "OBS bridge stopped")
            return
        fut.add_done_callback(lambda f: self._done.emit(cb, *self._result(f)))

    @staticmethod
    def _result(f):
        try:
            return f.result()
        except asyncio.CancelledError:
            return False, "cancelled"
        except Exception as e:
            log.exception("request task failed")
            return False, str(e)

    def _finish(self, cb, ok, payload):
        if cb:
            try:
                cb(ok, payload)
            except Exception:
                log.exception("response callback failed")

    def reconnect(self):
        loop = self._thread.loop
        if loop and self.client:
            loop.call_soon_threadsafe(self.client.reconnect_now)
