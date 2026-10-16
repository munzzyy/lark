"""Twitch sign-in (Device Code Grant, public client, no secret) and the few Helix calls Lark needs.

Tokens live in the keyring only. Pure asyncio, no Qt, so it can be tested against a fake server.
"""
import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger("lark.twitch")

ID_BASE = "https://id.twitch.tv"
API_BASE = "https://api.twitch.tv/helix"
MOD_SCOPES = ["moderator:manage:chat_messages", "moderator:manage:banned_users", "moderator:manage:warnings"]
SCOPES = ["channel:manage:broadcast", "chat:read", "chat:edit", "moderator:read:followers",
          "channel:read:subscriptions", "bits:read", *MOD_SCOPES]
TOKEN_KEY = "twitch"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_MARGIN = 120


class TwitchError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def blank_state():
    return {"status": "off", "message": "", "user_code": "", "verification_uri": "", "login": "", "user_id": "",
            "scopes": [], "channel": None}


class TwitchClient:
    def __init__(self, get_client_id, store, on_update=None, id_base=ID_BASE, api_base=API_BASE,
                 clock=time.time, sleep=asyncio.sleep):
        self._client_id = get_client_id
        self._store = store
        self._on_update = on_update or (lambda s: None)
        self._id, self._api = id_base, api_base
        self._clock, self._sleep = clock, sleep
        self.state = blank_state()
        self._tok = None
        self._lock = asyncio.Lock()
        self._flow = None

    def _emit(self, **kw):
        self.state = {**self.state, **kw}
        self._on_update(dict(self.state))

    @property
    def connected(self):
        return self.state["status"] == "connected"

    @property
    def can_send_chat(self):
        return self.connected and "chat:edit" in self.state["scopes"]

    async def _http(self, method, url, **kw):
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, **kw) as r:
                    text = await r.text()
                    try:
                        body = json.loads(text) if text else {}
                    except ValueError:
                        body = {"message": text[:200]}
                    return r.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise TwitchError(f"Could not reach Twitch ({type(e).__name__})") from None

    def _need_client_id(self):
        cid = self._client_id()
        if not cid:
            raise TwitchError("No Twitch client id set")
        return cid

    # sign-in

    async def restore(self):
        """Pick up a stored token at startup. Quietly stays off if there is none or no client id."""
        if not self._client_id():
            return
        try:
            raw = await asyncio.to_thread(self._store.lookup, TOKEN_KEY)
        except Exception as e:
            log.warning("keyring lookup failed: %s", e)
            return
        if not raw:
            return
        try:
            tok = json.loads(raw)
            if tok.get("client_id") != self._client_id() or not tok.get("refresh_token"):
                return
        except ValueError:
            log.warning("stored Twitch token is unreadable, ignoring it")
            return
        self._tok = tok
        try:
            await self.access_token()
            await self._finish_login(self._tok)
        except TwitchError as e:
            self._tok = None
            self._emit(status="error", message=f"Twitch sign-in expired: {e}")

    async def connect(self):
        """Run the device flow to the end. Updates go out through on_update."""
        cid = self._need_client_id()
        self._flow = asyncio.current_task()
        try:
            status, d = await self._http("POST", f"{self._id}/oauth2/device",
                                         data={"client_id": cid, "scopes": " ".join(SCOPES)})
            if status != 200 or "device_code" not in d:
                raise TwitchError(d.get("message") or f"Twitch refused the request ({status})", status)
            self._emit(status="pending", message="Waiting for you to approve it on Twitch",
                       user_code=d["user_code"], verification_uri=d["verification_uri"])
            deadline = self._clock() + int(d.get("expires_in", 300))
            interval = max(1, int(d.get("interval", 5)))
            while True:
                await self._sleep(interval)
                if self._clock() > deadline:
                    raise TwitchError("The code expired, start again")
                status, t = await self._http("POST", f"{self._id}/oauth2/token", data={
                    "client_id": cid, "scopes": " ".join(SCOPES), "device_code": d["device_code"],
                    "grant_type": DEVICE_GRANT})
                if status == 200 and "access_token" in t:
                    break
                msg = str(t.get("message", ""))
                if msg == "authorization_pending":
                    continue
                if msg == "slow_down":
                    interval += 5
                    continue
                raise TwitchError(f"Sign-in failed: {msg or status}", status)
            tok = self._token_from(t, cid)
            self._tok = tok
            await self._finish_login(tok)
        except asyncio.CancelledError:
            self._emit(**{**blank_state()})
            raise
        except TwitchError as e:
            self._tok = None
            self._emit(**{**blank_state(), "status": "error", "message": str(e)})
        finally:
            self._flow = None

    def cancel(self):
        if self._flow:
            self._flow.cancel()

    def _token_from(self, t, cid, old=None):
        return {"client_id": cid, "access_token": t["access_token"],
                "refresh_token": t.get("refresh_token") or (old or {}).get("refresh_token", ""),
                "expires_at": self._clock() + int(t.get("expires_in", 14400)),
                "scopes": t.get("scope") or (old or {}).get("scopes") or SCOPES,
                "login": (old or {}).get("login", ""), "user_id": (old or {}).get("user_id", "")}

    async def _finish_login(self, tok):
        status, v = await self._http("GET", f"{self._id}/oauth2/validate",
                                     headers={"Authorization": f"OAuth {tok['access_token']}"})
        if status != 200:
            raise TwitchError(v.get("message") or "Twitch did not accept the token", status)
        tok["login"], tok["user_id"] = v.get("login", ""), v.get("user_id", "")
        tok["scopes"] = v.get("scopes") or tok["scopes"]
        await self._save(tok)
        self._emit(status="connected", message="", user_code="", verification_uri="", login=tok["login"],
                   user_id=tok["user_id"], scopes=list(tok["scopes"]))
        try:
            await self.get_channel()
        except TwitchError as e:
            log.warning("could not read the channel: %s", e)

    async def _save(self, tok):
        try:
            await asyncio.to_thread(self._store.store, TOKEN_KEY, json.dumps(tok))
        except Exception as e:
            raise TwitchError(f"Signed in, but the keyring would not store the token ({e})") from None

    async def disconnect(self):
        self.cancel()
        self._tok = None
        try:
            await asyncio.to_thread(self._store.clear, TOKEN_KEY)
        except Exception as e:
            log.warning("keyring clear failed: %s", e)
        self._emit(**blank_state())

    async def access_token(self):
        """A usable access token, refreshed first if it is close to expiring. Refresh tokens are one-use, so this is locked."""
        async with self._lock:
            tok = self._tok
            if not tok:
                raise TwitchError("Not signed in to Twitch")
            if tok["expires_at"] - self._clock() > REFRESH_MARGIN:
                return tok["access_token"]
            cid = self._need_client_id()
            status, t = await self._http("POST", f"{self._id}/oauth2/token", data={
                "client_id": cid, "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            if status != 200 or "access_token" not in t:
                self._tok = None
                await asyncio.to_thread(self._store.clear, TOKEN_KEY)
                self._emit(**{**blank_state(), "status": "error", "message": "Twitch sign-in expired, connect again"})
                raise TwitchError(str(t.get("message") or "refresh failed"), status)
            new = self._token_from(t, cid, tok)
            self._tok = new
            await self._save(new)
            return new["access_token"]

    async def chat_auth(self):
        """(login, 'oauth:token') for IRC, or None when not signed in with chat:edit."""
        if not self.can_send_chat:
            return None
        try:
            return self._tok["login"], f"oauth:{await self.access_token()}"
        except TwitchError:
            return None

    # Helix

    async def _helix(self, method, path, **kw):
        for attempt in (0, 1):
            token = await self.access_token()
            headers = {"Authorization": f"Bearer {token}", "Client-Id": self._need_client_id()}
            status, body = await self._http(method, f"{self._api}{path}", headers=headers, **kw)
            if status == 401 and attempt == 0:
                self._tok["expires_at"] = 0
                continue
            if status >= 400:
                raise TwitchError(str(body.get("message") or f"Twitch answered {status}"), status)
            return body

    async def get_channel(self):
        uid = self.state["user_id"]
        body = await self._helix("GET", "/channels", params={"broadcaster_id": uid})
        rows = body.get("data") or []
        if not rows:
            raise TwitchError("Twitch returned no channel")
        ch = {"title": rows[0].get("title", ""), "game_id": rows[0].get("game_id", ""),
              "game_name": rows[0].get("game_name", "")}
        self._emit(channel=ch)
        return ch

    async def set_channel(self, title, game_id=None):
        title = " ".join(title.split())
        if not title:
            raise TwitchError("The title can't be empty")
        if len(title) > 140:
            raise TwitchError("Twitch titles are 140 characters at most")
        payload = {"title": title}
        if game_id:
            payload["game_id"] = str(game_id)
        await self._helix("PATCH", "/channels", params={"broadcaster_id": self.state["user_id"]}, json=payload)
        return await self.get_channel()

    async def search_categories(self, query):
        query = query.strip()
        if not query:
            return []
        body = await self._helix("GET", "/search/categories", params={"query": query, "first": 8})
        return [{"id": r["id"], "name": r["name"]} for r in body.get("data", []) if "id" in r and "name" in r]
