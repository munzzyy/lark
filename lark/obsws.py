"""obs-websocket v5 client on asyncio. No Qt in here."""
import asyncio
import base64
import hashlib
import json
import logging
import uuid

import aiohttp

log = logging.getLogger("lark.obsws")

SUBPROTOCOL = "obswebsocket.json"

# event subscription bits
GENERAL = 1 << 0
CONFIG = 1 << 1
SCENES = 1 << 2
INPUTS = 1 << 3
OUTPUTS = 1 << 6
SCENE_ITEMS = 1 << 7
INPUT_VOLUME_METERS = 1 << 16
DEFAULT_SUBSCRIPTIONS = GENERAL | CONFIG | SCENES | INPUTS | OUTPUTS | SCENE_ITEMS | INPUT_VOLUME_METERS

CLOSE_AUTH_FAILED = 4009


class ObsError(Exception):
    def __init__(self, comment, code=-1):
        super().__init__(comment)
        self.code = code
        self.comment = comment


def make_auth(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    """Connects, identifies, and keeps reconnecting until stop().

    get_conn() is called on every attempt and returns (host, port, password),
    so a settings change takes effect on the next retry or on reconnect_now().
    """

    def __init__(self, get_conn, on_event=None, on_state=None, subscriptions=DEFAULT_SUBSCRIPTIONS,
                 backoff=(0.5, 1, 2, 4, 8, 10), request_timeout=5.0):
        self._get_conn = get_conn
        self._on_event = on_event or (lambda t, d: None)
        self._on_state = on_state or (lambda s, d: None)
        self._subs = subscriptions
        self._backoff = backoff
        self._timeout = request_timeout
        self._ws = None
        self._pending = {}
        self._stop = False
        self._wake = None
        self.state = "disconnected"

    def _set_state(self, state, detail=""):
        self.state = state
        try:
            self._on_state(state, detail)
        except Exception:
            log.exception("state callback failed")

    @property
    def connected(self):
        return self.state == "connected"

    async def run(self):
        self._wake = asyncio.Event()
        attempt = 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_connect=5)) as session:
            while not self._stop:
                self._wake.clear()
                host, port, password = self._get_conn()
                self._set_state("connecting", f"{host}:{port}")
                got_in = False
                try:
                    got_in = await self._session(session, host, port, password)
                except asyncio.CancelledError:
                    raise
                except AuthFailed:
                    self._set_state("auth_failed", "OBS rejected the password")
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ObsError, _Closed) as e:
                    self._set_state("disconnected", f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                else:
                    self._set_state("disconnected", "connection closed")
                self._fail_pending("disconnected")
                self._ws = None
                if self._stop:
                    break
                attempt = 0 if got_in else attempt + 1
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
        self._set_state("disconnected", "stopped")

    async def _session(self, session, host, port, password):
        url = f"ws://{host}:{port}"
        async with session.ws_connect(url, protocols=(SUBPROTOCOL,), timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                      max_msg_size=8 * 1024 * 1024) as ws:
            hello = await self._recv(ws, 5)
            if hello.get("op") != 0:
                raise ObsError("expected Hello")
            ident = {"rpcVersion": 1, "eventSubscriptions": self._subs}
            auth = hello["d"].get("authentication")
            if auth:
                ident["authentication"] = make_auth(password, auth["salt"], auth["challenge"])
            await ws.send_json({"op": 1, "d": ident})
            try:
                reply = await self._recv(ws, 5)
            except _Closed as e:
                if e.code == CLOSE_AUTH_FAILED:
                    raise AuthFailed() from None
                raise
            if reply.get("op") != 2:
                raise ObsError("identify failed")
            self._ws = ws
            self._set_state("connected", f"{host}:{port}")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.ERROR):
                    break
            if ws.close_code == CLOSE_AUTH_FAILED:
                raise AuthFailed()
        return True

    async def _recv(self, ws, timeout):
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            return json.loads(msg.data)
        raise _Closed(ws.close_code)

    def _dispatch(self, raw):
        try:
            m = json.loads(raw)
            op, d = m["op"], m["d"]
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring malformed message from OBS")
            return
        if op == 5:
            try:
                self._on_event(d.get("eventType", ""), d.get("eventData") or {})
            except Exception:
                log.exception("event callback failed")
        elif op == 7:
            fut = self._pending.pop(d.get("requestId"), None)
            if fut is None or fut.done():
                return
            st = d.get("requestStatus", {})
            if st.get("result"):
                fut.set_result(d.get("responseData") or {})
            else:
                fut.set_exception(ObsError(st.get("comment") or "request failed", st.get("code", -1)))

    def _fail_pending(self, why):
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ObsError(why))
        self._pending.clear()

    async def request(self, req_type, data=None, timeout=None):
        ws = self._ws
        if ws is None or ws.closed:
            raise ObsError("not connected")
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"requestType": req_type, "requestId": rid}
        if data:
            payload["requestData"] = data
        try:
            await ws.send_json({"op": 6, "d": payload})
            return await asyncio.wait_for(fut, timeout or self._timeout)
        except asyncio.TimeoutError:
            raise ObsError(f"{req_type} timed out") from None
        finally:
            self._pending.pop(rid, None)

    def reconnect_now(self):
        """Call from the loop thread."""
        if self._wake is not None:
            self._wake.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            asyncio.ensure_future(ws.close())

    async def stop(self):
        self._stop = True
        if self._wake is not None:
            self._wake.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()


class AuthFailed(Exception):
    pass


class _Closed(Exception):
    def __init__(self, code):
        super().__init__(f"closed ({code})")
        self.code = code
