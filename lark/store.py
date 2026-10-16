"""Everything Lark knows about OBS, kept in one place so every page reads the same state."""
import logging
import os
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .camwatch import CamWatch

log = logging.getLogger("lark.store")

SIGNAL_FLOOR_DB = -60.0
SIGNAL_WINDOW_S = 4.0


class Store(QObject):
    conn_changed = Signal(str, str)
    scenes_changed = Signal()
    items_changed = Signal(str)
    outputs_changed = Signal()
    stats_changed = Signal()
    inputs_changed = Signal()
    input_updated = Signal(str)
    meters = Signal(object)
    toast = Signal(str, bool)
    cam_light_changed = Signal()
    sticky_show = Signal(str, str, str)
    sticky_hide = Signal(str)
    sticky_clicked = Signal(str)

    def __init__(self, bridge, config, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.config = config
        self.conn = "disconnected"
        self.conn_detail = ""
        self.scenes = []
        self.current_scene = ""
        self.items = {}
        self.inputs = {}
        self.stream = {"active": False, "reconnecting": False, "duration_ms": 0, "skipped": 0, "total": 0,
                       "congestion": 0.0}
        self.record = {"active": False, "paused": False}
        self.replay = False
        self.vcam = False
        self.vcam_available = None
        self.stats = {}
        self.kbps = 0.0
        self.stream_key_set = None
        self._last_bytes = None
        self._last_bytes_t = 0.0
        self._last_signal = {}
        self._got_meters = False
        self.cam_luma = None
        self.cam_dark = False
        self.camwatch = CamWatch(self, config)

        bridge.state.connect(self._on_state)
        bridge.event.connect(self._on_event)
        bridge.meters.connect(self._on_meters)
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._poll_tick)

    @property
    def connected(self):
        return self.conn == "connected"

    def _err(self, what):
        def cb(ok, payload):
            if not ok:
                self.toast.emit(f"{what}: {payload}", True)
        return cb

    def _on_state(self, state, detail):
        was = self.connected
        self.conn, self.conn_detail = state, detail
        if self.connected and not was:
            self._poll.start()
            self.refresh_all()
            self.camwatch.start()
        elif not self.connected:
            self._poll.stop()
            self.camwatch.stop()
            self.stream["active"] = False
            self.record["active"] = False
            self._last_bytes = None
            self._got_meters = False
            self.outputs_changed.emit()
        self.conn_changed.emit(state, detail)

    def refresh_all(self):
        self.refresh_scenes()
        self.refresh_inputs()
        self.refresh_outputs()
        self.bridge.request("GetStreamServiceSettings", None, self._got_service)

    def _got_service(self, ok, p):
        if ok:
            key = (p.get("streamServiceSettings") or {}).get("key")
            self.stream_key_set = bool(key)
        else:
            self.stream_key_set = None
        self.outputs_changed.emit()

    def refresh_outputs(self):
        self.bridge.request("GetStreamStatus", None, self._got_stream)
        self.bridge.request("GetRecordStatus", None, self._got_record)
        self.bridge.request("GetReplayBufferStatus", None,
                            lambda ok, p: self._set_flag("replay", ok and p.get("outputActive", False)))
        self.bridge.request("GetVirtualCamStatus", None, self._got_vcam)

    def _got_vcam(self, ok, p):
        self.vcam_available = ok
        self._set_flag("vcam", ok and p.get("outputActive", False))

    def _set_flag(self, name, val):
        setattr(self, name, bool(val))
        self.outputs_changed.emit()

    def _got_stream(self, ok, p):
        if not ok:
            return
        now = time.monotonic()
        s = self.stream
        s["active"] = p.get("outputActive", False)
        s["reconnecting"] = p.get("outputReconnecting", False)
        s["duration_ms"] = p.get("outputDuration", 0)
        s["skipped"] = p.get("outputSkippedFrames", 0)
        s["total"] = p.get("outputTotalFrames", 0)
        s["congestion"] = p.get("outputCongestion", 0.0)
        b = p.get("outputBytes", 0)
        if s["active"] and self._last_bytes is not None and now > self._last_bytes_t and b >= self._last_bytes:
            self.kbps = (b - self._last_bytes) * 8 / 1000 / (now - self._last_bytes_t)
        elif not s["active"]:
            self.kbps = 0.0
        self._last_bytes, self._last_bytes_t = (b, now) if s["active"] else (None, 0.0)
        self.outputs_changed.emit()

    def _got_record(self, ok, p):
        if ok:
            self.record = {"active": p.get("outputActive", False), "paused": p.get("outputPaused", False)}
            self.outputs_changed.emit()

    def _poll_tick(self):
        self.bridge.request("GetStreamStatus", None, self._got_stream)
        self.bridge.request("GetStats", None, self._got_stats)

    def _got_stats(self, ok, p):
        if ok:
            self.stats = p
            self.stats_changed.emit()

    @property
    def dropped_pct(self):
        t = self.stream["total"]
        return 100.0 * self.stream["skipped"] / t if t else 0.0

    def refresh_scenes(self):
        self.bridge.request("GetSceneList", None, self._got_scenes)

    def _got_scenes(self, ok, p):
        if not ok:
            return
        scenes = sorted(p.get("scenes", []), key=lambda s: -s.get("sceneIndex", 0))
        self.scenes = [s["sceneName"] for s in scenes]
        self.current_scene = p.get("currentProgramSceneName", "")
        self.items = {k: v for k, v in self.items.items() if k in self.scenes}
        self.scenes_changed.emit()
        for s in self.scenes:
            self.refresh_items(s)

    def refresh_items(self, scene):
        def cb(ok, p):
            if ok:
                self.items[scene] = [{"id": i["sceneItemId"], "name": i["sourceName"],
                                      "enabled": i["sceneItemEnabled"]}
                                     for i in sorted(p.get("sceneItems", []), key=lambda i: -i.get("sceneItemIndex", 0))]
                self.items_changed.emit(scene)
        self.bridge.request("GetSceneItemList", {"sceneName": scene}, cb)

    def refresh_inputs(self):
        self.bridge.request("GetInputList", None, self._got_inputs)

    def _got_inputs(self, ok, p):
        if not ok:
            return
        names = [i["inputName"] for i in p.get("inputs", [])]
        self.inputs = {n: v for n, v in self.inputs.items() if n in names}
        pending = {"n": len(names)}

        def one_done():
            pending["n"] -= 1
            if pending["n"] <= 0:
                self.inputs_changed.emit()

        if not names:
            self.inputs_changed.emit()
        for n in names:
            def vol(ok, r, n=n):
                if not ok:
                    self.inputs.pop(n, None)
                    one_done()
                    return
                entry = self.inputs.setdefault(n, {"muted": False})
                entry["db"] = r.get("inputVolumeDb", 0.0)

                def mute(ok2, r2):
                    if ok2:
                        entry["muted"] = r2.get("inputMuted", False)
                    one_done()
                self.bridge.request("GetInputMute", {"inputName": n}, mute)
            self.bridge.request("GetInputVolume", {"inputName": n}, vol)

    def _on_event(self, t, d):
        if t == "CurrentProgramSceneChanged":
            self.current_scene = d.get("sceneName", "")
            self.scenes_changed.emit()
        elif t in ("SceneListChanged", "SceneCreated", "SceneRemoved", "SceneNameChanged"):
            self.refresh_scenes()
        elif t == "SceneItemEnableStateChanged":
            for it in self.items.get(d.get("sceneName"), []):
                if it["id"] == d.get("sceneItemId"):
                    it["enabled"] = d.get("sceneItemEnabled", it["enabled"])
            self.items_changed.emit(d.get("sceneName", ""))
        elif t in ("SceneItemCreated", "SceneItemRemoved", "SceneItemListReindexed"):
            self.refresh_items(d.get("sceneName", ""))
        elif t == "StreamStateChanged":
            self.stream["active"] = d.get("outputActive", False)
            if not self.stream["active"]:
                self.kbps = 0.0
                self._last_bytes = None
            self.outputs_changed.emit()
            self.refresh_outputs()
        elif t == "RecordStateChanged":
            self.record["active"] = d.get("outputActive", False)
            self.outputs_changed.emit()
            if not d.get("outputActive") and d.get("outputPath"):
                self.toast.emit("Recording saved", False)
        elif t == "ReplayBufferStateChanged":
            self._set_flag("replay", d.get("outputActive", False))
        elif t == "ReplayBufferSaved":
            self.toast.emit("Replay saved", False)
        elif t == "VirtualcamStateChanged":
            self._set_flag("vcam", d.get("outputActive", False))
        elif t == "InputVolumeChanged" and d.get("inputName") in self.inputs:
            self.inputs[d["inputName"]]["db"] = d.get("inputVolumeDb", 0.0)
            self.input_updated.emit(d["inputName"])
        elif t == "InputMuteStateChanged" and d.get("inputName") in self.inputs:
            self.inputs[d["inputName"]]["muted"] = d.get("inputMuted", False)
            self.input_updated.emit(d["inputName"])
        elif t in ("InputCreated", "InputRemoved", "InputNameChanged"):
            self.refresh_inputs()
        elif t == "ExitStarted":
            self.toast.emit("OBS is closing", False)

    def _on_meters(self, levels):
        self._got_meters = True
        now = time.monotonic()
        for name, (_, peak) in levels.items():
            if peak > SIGNAL_FLOOR_DB:
                self._last_signal[name] = now
        self.meters.emit(levels)

    def has_signal(self, name):
        if not self._got_meters:
            return None
        return time.monotonic() - self._last_signal.get(name, -1e9) < SIGNAL_WINDOW_S

    def camera_present(self):
        return os.path.exists(self.config.get("camera_device"))

    # actions

    def switch_scene(self, name):
        self.bridge.request("SetCurrentProgramScene", {"sceneName": name}, self._err("Switch scene"))

    def set_item_enabled(self, scene, item_id, on):
        self.bridge.request("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id,
                                                    "sceneItemEnabled": on}, self._err("Toggle source"))

    def set_volume(self, name, db):
        self.inputs.get(name, {})["db"] = db
        self.bridge.request("SetInputVolume", {"inputName": name, "inputVolumeDb": db}, self._err("Set volume"))

    def set_mute(self, name, muted):
        self.inputs.get(name, {})["muted"] = muted
        self.input_updated.emit(name)
        self.bridge.request("SetInputMute", {"inputName": name, "inputMuted": muted}, self._err("Mute"))

    def start_stream(self):
        self.bridge.request("StartStream", None, self._err("Go live"))

    def stop_stream(self):
        self.bridge.request("StopStream", None, self._err("Stop stream"))

    def toggle_record(self):
        self.bridge.request("StopRecord" if self.record["active"] else "StartRecord", None, self._err("Record"))

    def save_replay(self):
        if not self.replay:
            self.bridge.request("StartReplayBuffer", None, self._err("Replay buffer"))
            self.toast.emit("Replay buffer started, press again once there is something to save", False)
        else:
            self.bridge.request("SaveReplayBuffer", None, self._err("Save replay"))

    def toggle_vcam(self):
        self.bridge.request("StopVirtualCam" if self.vcam else "StartVirtualCam", None, self._err("Virtual camera"))

    def add_browser_source(self, name, url, width, height):
        scene = self.current_scene
        if not scene:
            self.toast.emit("No current scene to add it to", True)
            return
        settings = {"url": url, "width": width, "height": height, "css": "", "reroute_audio": False}

        def cb(ok, p):
            if ok:
                self.toast.emit(f"Added {name} to {scene}", False)
            else:
                self.toast.emit(f"Could not add {name}: {p}", True)
        self.bridge.request("CreateInput", {"sceneName": scene, "inputName": name, "inputKind": "browser_source",
                                            "inputSettings": settings, "sceneItemEnabled": True}, cb)
