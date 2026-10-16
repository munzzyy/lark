"""A small stateful fake of obs-websocket v5, for tests and for screenshots.

Run standalone:  python3 -m tests.fake_obs --port 4456 --password hunter2
"""
import argparse
import asyncio
import base64
import json
import math
import os
import random
import secrets
import time

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from aiohttp import web, WSMsgType

from lark.obsws import make_auth


class FakeObs:
    def __init__(self, password="pw", meters=False, vcam_supported=True):
        self.vcam_supported = vcam_supported
        self.password = password
        self.send_meters = meters
        self.scenes = {
            "Game": [("Game capture", True), ("Camera", True)],
            "Starting Soon": [("Title card", True), ("Camera", False)],
            "BRB": [("BRB card", True)],
            "Camera full": [("Camera", True)],
        }
        self.scene_order = list(self.scenes)
        self.current = "Game"
        self.item_ids = {}
        n = 1
        for s, items in self.scenes.items():
            for name, _ in items:
                self.item_ids[(s, name)] = n
                n += 1
        self.next_id = n
        self.inputs = {
            "Mic (Fifine)": {"kind": "pulse_input_capture", "db": -6.0, "mute": False},
            "Desktop Audio": {"kind": "pulse_output_capture", "db": -12.0, "mute": False},
            "Camera": {"kind": "v4l2_input", "db": None, "mute": None, "settings": {
                "device_id": "/dev/video0",
                "pixelformat": 1196444237, "resolution": 125830200, "framerate": 65596,
                "buffering": False, "auto_reset": False}},
        }
        self.streaming = False
        self.stream_started = 0.0
        self.recording = False
        self.replay = False
        self.vcam = False
        self.stream_key = "live_x"
        self.camera_luma = 100
        self.camera_dark_after = None
        self.started_at = time.time()
        self.requests = []
        self.clients = []
        self.connections = 0
        self.auth_failures = 0
        self.stream_bytes = 0
        self.replay_paths = []
        self.last_replay = None
        self.transforms = {}
        self.filters = {"Camera": [{"filterName": "Someone's Denoise", "filterKind": "denoise_filter",
                                    "filterEnabled": True, "filterSettings": {"keep": 1}}]}
        self.input_kinds = ["browser_source", "text_ft2_source", "v4l2_input", "pipewire-screen-capture-source",
                            "pipewire-window-capture-source", "pulse_input_capture", "pulse_output_capture"]

    def camera_png(self, width=160):
        """A PNG whose centre half is the current camera_luma gray and whose border is white, so a test can
        tell the centre crop from a whole-frame mean."""
        luma = self.camera_luma
        if self.camera_dark_after is not None and time.time() - self.started_at > self.camera_dark_after:
            luma = 16
        h = width * 9 // 16
        img = QImage(width, h, QImage.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        for y in range(h // 4, h - h // 4):
            for x in range(width // 4, width - width // 4):
                img.setPixelColor(x, y, QColor(luma, luma, luma))
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    def app(self):
        a = web.Application()
        a.router.add_get("/", self.handle_ws)
        return a

    async def start(self, port=0, host="127.0.0.1"):
        self.runner = web.AppRunner(self.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.meter_task = asyncio.ensure_future(self._meter_loop())
        return self.port

    async def stop(self):
        self.meter_task.cancel()
        for ws in list(self.clients):
            await ws.close()
        await asyncio.sleep(0.1)  # let half-accepted connections finish, see fake_twitch.stop
        await self.runner.cleanup()

    async def drop_clients(self):
        for ws in list(self.clients):
            await ws.close()

    async def broadcast(self, event_type, data, intent=1):
        msg = {"op": 5, "d": {"eventType": event_type, "eventIntent": intent, "eventData": data}}
        for ws in list(self.clients):
            await ws.send_json(msg)

    async def _meter_loop(self):
        t0 = time.time()
        while True:
            await asyncio.sleep(0.05)
            if not self.send_meters or not self.clients:
                continue
            t = time.time() - t0
            lv = []
            for name, i in self.inputs.items():
                if i["db"] is None:
                    continue
                mag = 0.0 if i["mute"] else max(0.0, 0.25 + 0.2 * math.sin(t * (2 if "Mic" in name else 0.7)) + random.random() * 0.1)
                lv.append({"inputName": name, "inputUuid": name, "inputLevelsMul": [[mag, min(1, mag * 1.2), min(1, mag * 1.2)]] * 2})
            await self.broadcast("InputVolumeMeters", {"inputs": lv}, 1 << 16)

    async def handle_ws(self, req):
        ws = web.WebSocketResponse(protocols=("obswebsocket.json",))
        await ws.prepare(req)
        self.connections += 1
        challenge, salt = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        hello = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
        if self.password is not None:
            hello["authentication"] = {"challenge": challenge, "salt": salt}
        await ws.send_json({"op": 0, "d": hello})
        msg = await ws.receive()
        if msg.type != WSMsgType.TEXT:
            return ws
        ident = json.loads(msg.data)["d"]
        if self.password is not None and ident.get("authentication") != make_auth(self.password, salt, challenge):
            self.auth_failures += 1
            await ws.close(code=4009, message=b"Authentication failed.")
            return ws
        await ws.send_json({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        self.clients.append(ws)
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d["op"] != 6:
                    continue
                r = d["d"]
                self.requests.append((r["requestType"], r.get("requestData") or {}))
                status, body = await self.handle_request(r["requestType"], r.get("requestData") or {})
                await ws.send_json({"op": 7, "d": {
                    "requestType": r["requestType"], "requestId": r["requestId"],
                    "requestStatus": status, "responseData": body}})
        finally:
            if ws in self.clients:
                self.clients.remove(ws)
        return ws

    @staticmethod
    def ok(body=None):
        return {"result": True, "code": 100}, body or {}

    @staticmethod
    def fail(code, comment):
        return {"result": False, "code": code, "comment": comment}, None

    def _items(self, scene):
        return [{"sceneItemId": self.item_ids[(scene, n)], "sourceName": n, "sceneItemEnabled": e,
                 "sceneItemIndex": i} for i, (n, e) in enumerate(self.scenes[scene])]

    async def handle_request(self, t, d):
        if t == "GetSceneList":
            return self.ok({"currentProgramSceneName": self.current,
                            "scenes": [{"sceneName": s, "sceneIndex": i} for i, s in enumerate(reversed(self.scene_order))]})
        if t == "SetCurrentProgramScene":
            if d.get("sceneName") not in self.scenes:
                return self.fail(600, "No scene was found by that name.")
            self.current = d["sceneName"]
            await self.broadcast("CurrentProgramSceneChanged", {"sceneName": self.current}, 4)
            return self.ok()
        if t == "GetSceneItemList":
            if d.get("sceneName") not in self.scenes:
                return self.fail(600, "No scene was found by that name.")
            return self.ok({"sceneItems": self._items(d["sceneName"])})
        if t == "SetSceneItemEnabled":
            s = d["sceneName"]
            for i, (n, e) in enumerate(self.scenes[s]):
                if self.item_ids[(s, n)] == d["sceneItemId"]:
                    self.scenes[s][i] = (n, d["sceneItemEnabled"])
                    await self.broadcast("SceneItemEnableStateChanged", {
                        "sceneName": s, "sceneItemId": d["sceneItemId"], "sceneItemEnabled": d["sceneItemEnabled"]}, 128)
                    return self.ok()
            return self.fail(600, "No scene items were found.")
        if t == "GetStreamStatus":
            dur = int((time.time() - self.stream_started) * 1000) if self.streaming else 0
            if self.streaming:
                self.stream_bytes += 750_000
            return self.ok({"outputActive": self.streaming, "outputReconnecting": False,
                            "outputTimecode": "00:00:00.000", "outputDuration": dur, "outputCongestion": 0.0,
                            "outputBytes": self.stream_bytes, "outputSkippedFrames": 3 if self.streaming else 0,
                            "outputTotalFrames": 1000 if self.streaming else 0})
        if t in ("StartStream", "StopStream"):
            on = t == "StartStream"
            if on == self.streaming:
                return self.fail(500 + on, "already")
            self.streaming = on
            self.stream_started = time.time()
            await self.broadcast("StreamStateChanged", {"outputActive": on, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED" if on else "OBS_WEBSOCKET_OUTPUT_STOPPED"}, 64)
            return self.ok()
        if t == "GetRecordStatus":
            return self.ok({"outputActive": self.recording, "outputPaused": False, "outputTimecode": "00:00:00.000",
                            "outputDuration": 0, "outputBytes": 0})
        if t in ("StartRecord", "StopRecord"):
            on = t == "StartRecord"
            self.recording = on
            await self.broadcast("RecordStateChanged", {"outputActive": on, "outputState": "x", "outputPath": "/tmp/x.mkv"}, 64)
            return self.ok({"outputPath": "/tmp/x.mkv"} if not on else {})
        if t == "GetReplayBufferStatus":
            return self.ok({"outputActive": self.replay})
        if t in ("StartReplayBuffer", "StopReplayBuffer"):
            self.replay = t == "StartReplayBuffer"
            await self.broadcast("ReplayBufferStateChanged", {"outputActive": self.replay, "outputState": "x"}, 64)
            return self.ok()
        if t == "SaveReplayBuffer":
            if not self.replay:
                return self.fail(500, "replay buffer is not active")
            self.last_replay = self.replay_paths.pop(0) if self.replay_paths else "/tmp/r.mkv"
            await self.broadcast("ReplayBufferSaved", {"savedReplayPath": self.last_replay}, 64)
            return self.ok()
        if t == "GetLastReplayBufferReplay":
            if not self.replay:
                return self.fail(500, "replay buffer is not active")
            if self.last_replay is None:
                return self.fail(501, "no replay has been saved")
            return self.ok({"savedReplayPath": self.last_replay})
        if t in ("GetVirtualCamStatus", "StartVirtualCam", "StopVirtualCam") and not self.vcam_supported:
            return self.fail(604, "VirtualCam is not available.")
        if t == "GetVirtualCamStatus":
            return self.ok({"outputActive": self.vcam})
        if t in ("StartVirtualCam", "StopVirtualCam"):
            self.vcam = t == "StartVirtualCam"
            await self.broadcast("VirtualcamStateChanged", {"outputActive": self.vcam, "outputState": "x"}, 64)
            return self.ok()
        if t == "GetStats":
            return self.ok({"cpuUsage": 7.5, "memoryUsage": 512.0, "availableDiskSpace": 100000.0, "activeFps": 60.0,
                            "averageFrameRenderTime": 2.1, "renderSkippedFrames": 0, "renderTotalFrames": 5000,
                            "outputSkippedFrames": 0, "outputTotalFrames": 5000})
        if t == "GetInputList":
            return self.ok({"inputs": [{"inputName": n, "inputKind": i["kind"], "unversionedInputKind": i["kind"]}
                                       for n, i in self.inputs.items()]})
        if t in ("GetInputVolume", "GetInputMute", "SetInputVolume", "SetInputMute"):
            i = self.inputs.get(d.get("inputName"))
            if i is None:
                return self.fail(600, "No input was found by that name.")
            if i["db"] is None:
                return self.fail(604, "The input does not support audio.")
            if t == "GetInputVolume":
                return self.ok({"inputVolumeMul": 10 ** (i["db"] / 20), "inputVolumeDb": i["db"]})
            if t == "GetInputMute":
                return self.ok({"inputMuted": i["mute"]})
            if t == "SetInputVolume":
                i["db"] = d["inputVolumeDb"]
                await self.broadcast("InputVolumeChanged", {"inputName": d["inputName"], "inputVolumeDb": i["db"],
                                                            "inputVolumeMul": 10 ** (i["db"] / 20)}, 8)
                return self.ok()
            i["mute"] = d["inputMuted"]
            await self.broadcast("InputMuteStateChanged", {"inputName": d["inputName"], "inputMuted": i["mute"]}, 8)
            return self.ok()
        if t == "GetStreamServiceSettings":
            return self.ok({"streamServiceType": "rtmp_common",
                            "streamServiceSettings": {"service": "Twitch", "server": "auto", "key": self.stream_key}})
        if t == "CreateInput":
            s = d["sceneName"]
            if s not in self.scenes:
                return self.fail(600, "No scene was found by that name.")
            if d["inputName"] in self.inputs or any(n == d["inputName"] for n, _ in self.scenes[s]):
                return self.fail(601, "A source already exists by that name.")
            self.scenes[s].append((d["inputName"], d.get("sceneItemEnabled", True)))
            self.item_ids[(s, d["inputName"])] = self.next_id
            self.next_id += 1
            self.inputs[d["inputName"]] = {"kind": d["inputKind"], "db": None, "mute": None,
                                           "settings": d.get("inputSettings")}
            return self.ok({"sceneItemId": self.next_id - 1})
        if t == "GetInputKindList":
            return self.ok({"inputKinds": list(self.input_kinds)})
        if t == "CreateScene":
            if d["sceneName"] in self.scenes or d["sceneName"] in self.inputs:
                return self.fail(601, "A source already exists by that name.")
            self.scenes[d["sceneName"]] = []
            self.scene_order.append(d["sceneName"])
            await self.broadcast("SceneCreated", {"sceneName": d["sceneName"], "isGroup": False}, 4)
            return self.ok({"sceneUuid": d["sceneName"]})
        if t == "CreateSceneItem":
            s = d["sceneName"]
            if s not in self.scenes:
                return self.fail(600, "No scene was found by that name.")
            if d["sourceName"] not in self.inputs:
                return self.fail(600, "No source was found by that name.")
            if (s, d["sourceName"]) in self.item_ids:
                return self.fail(601, "That source is already in the scene.")
            self.scenes[s].append((d["sourceName"], d.get("sceneItemEnabled", True)))
            self.item_ids[(s, d["sourceName"])] = self.next_id
            self.next_id += 1
            return self.ok({"sceneItemId": self.next_id - 1})
        if t == "SetSceneItemTransform":
            s = d["sceneName"]
            if s not in self.scenes or d["sceneItemId"] not in {self.item_ids[(s, n)] for n, _ in self.scenes[s]}:
                return self.fail(600, "No scene items were found.")
            key = (s, d["sceneItemId"])
            self.transforms[key] = {**self.transforms.get(key, {}), **d["sceneItemTransform"]}
            return self.ok()
        if t == "GetSceneItemId":
            if d.get("sceneName") not in self.scenes:
                return self.fail(600, "No scene was found by that name.")
            i = self.item_ids.get((d["sceneName"], d.get("sourceName")))
            if i is None or d["sourceName"] not in [n for n, _ in self.scenes[d["sceneName"]]]:
                return self.fail(600, "No scene items were found.")
            return self.ok({"sceneItemId": i})
        if t == "GetSceneItemTransform":
            s = d["sceneName"]
            for n, _ in self.scenes.get(s, []):
                if self.item_ids[(s, n)] == d["sceneItemId"]:
                    res = (self.inputs.get(n, {}).get("settings") or {}).get("resolution", 125830200)
                    w, h = res >> 16, res & 0xFFFF
                    crop = n == "Camera" and s == "Game"
                    base = {"sourceWidth": w, "sourceHeight": h, "boundsType": "OBS_BOUNDS_SCALE_INNER",
                            "boundsWidth": 480.0, "boundsHeight": 270.0, "positionX": 24.0, "positionY": 786.0,
                            "cropLeft": 320 if crop else 0, "cropRight": 320 if crop else 0,
                            "cropTop": 180 if crop else 0, "cropBottom": 180 if crop else 0}
                    return self.ok({"sceneItemTransform": {**base, **self.transforms.get((s, d["sceneItemId"]), {})}})
            return self.fail(600, "No scene items were found.")
        if t == "GetSourceScreenshot":
            name = d.get("sourceName")
            if name not in self.inputs and name not in self.scenes:
                return self.fail(600, "No source was found by that name.")
            if d.get("imageFormat") != "png":
                return self.fail(400, "Unsupported image format.")
            png = self.camera_png(d.get("imageWidth") or 160)
            return self.ok({"imageData": "data:image/png;base64," + base64.b64encode(png).decode()})
        if t in ("GetInputSettings", "SetInputSettings"):
            i = self.inputs.get(d.get("inputName"))
            if i is None:
                return self.fail(600, "No input was found by that name.")
            if t == "GetInputSettings":
                return self.ok({"inputKind": i["kind"], "inputSettings": dict(i.get("settings") or {})})
            cur = dict(i.get("settings") or {}) if d.get("overlay", True) else {}
            cur.update(d["inputSettings"])
            i["settings"] = cur
            return self.ok()
        if t in ("GetSourceFilterList", "GetSourceFilter", "CreateSourceFilter", "SetSourceFilterSettings",
                 "SetSourceFilterEnabled", "RemoveSourceFilter"):
            return self.filter_request(t, d)
        return self.fail(204, "Unknown request type.")

    @property
    def input_settings(self):
        return {n: i["settings"] for n, i in self.inputs.items() if i.get("settings")}

    def filter_request(self, t, d):
        src = d.get("sourceName")
        if src not in self.inputs and src not in self.scenes:
            return self.fail(600, "No source was found by that name.")
        flist = self.filters.setdefault(src, [])
        found = next((f for f in flist if f["filterName"] == d.get("filterName")), None)
        if t == "GetSourceFilterList":
            return self.ok({"filters": [dict(f, filterIndex=i) for i, f in enumerate(flist)]})
        if t == "CreateSourceFilter":
            if found:
                return self.fail(601, "A filter already exists by that name.")
            flist.append({"filterName": d["filterName"], "filterKind": d["filterKind"], "filterEnabled": True,
                          "filterSettings": dict(d.get("filterSettings") or {})})
            return self.ok()
        if found is None:
            return self.fail(600, "No filter was found by that name.")
        if t == "GetSourceFilter":
            return self.ok(dict(found, filterIndex=flist.index(found)))
        if t == "SetSourceFilterSettings":
            if d.get("overlay", True):
                found["filterSettings"].update(d["filterSettings"])
            else:
                found["filterSettings"] = dict(d["filterSettings"])
        elif t == "SetSourceFilterEnabled":
            found["filterEnabled"] = d["filterEnabled"]
        else:
            flist.remove(found)
        return self.ok()


async def _main(a):
    f = FakeObs(password=a.password or None, meters=True, vcam_supported=a.vcam)
    f.camera_luma = a.camera_luma
    f.camera_dark_after = a.camera_dark_after
    f.streaming = a.live
    f.stream_started = time.time()
    port = await f.start(a.port)
    print(f"fake obs on {port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4456)
    ap.add_argument("--password", default="pw")
    ap.add_argument("--vcam", action="store_true", help="pretend virtual camera is supported (the Flatpak OBS says 604)")
    ap.add_argument("--camera-luma", type=int, default=int(os.environ.get("FAKE_OBS_CAMERA_LUMA", "100")))
    ap.add_argument("--camera-dark-after", type=float, default=float(os.environ["FAKE_OBS_DARK_AFTER"])
                    if os.environ.get("FAKE_OBS_DARK_AFTER") else None,
                    help="seconds after start before the camera image turns dark")
    ap.add_argument("--live", action="store_true", default=bool(os.environ.get("FAKE_OBS_LIVE")),
                    help="start with the stream running")
    args = ap.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass
