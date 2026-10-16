"""Pre-flight checks for going live. Pure functions, no Qt."""
from dataclasses import dataclass

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass
class Check:
    key: str
    label: str
    status: str
    detail: str = ""


def run_checks(*, connected, scene, mic_name, mic, mic_signal, camera_present, stream_key_set,
               twitch_connected=False, twitch_title="", camera_preset=None, camera_light=None):
    """mic is the input's dict ({'muted': bool}) or None if OBS has no such input.
    mic_signal is True/False, or None if no meter data has arrived yet.
    camera_preset is (number, loaded) from the camera library, or None when it cannot say.
    camera_light is (looks_dark, luma) from the OBS camera screenshots, or None before the first one.
    Only OBS being connected and a stream key being set block going live."""
    c = []
    c.append(Check("obs", "OBS connected", OK if connected else FAIL,
                   "" if connected else "Open OBS (Lark connects on its own)"))
    if not connected:
        for k, l in (("scene", "A scene is selected"), ("mic", "Mic is on and has signal"),
                     ("camera", "Camera is plugged in"), ("preset", "Camera preset is loaded"),
                     ("light", "Camera image looks dark"), ("key", "Stream key is set in OBS")):
            c.append(Check(k, l, SKIP, "Waiting for OBS"))
    else:
        c.append(Check("scene", "A scene is selected", OK if scene else FAIL,
                       scene or "OBS has no current scene"))
        if mic is None:
            c.append(Check("mic", "Mic is on and has signal", WARN, f"No input called {mic_name!r} in OBS"))
        elif mic.get("muted"):
            c.append(Check("mic", "Mic is on and has signal", WARN, f"{mic_name} is muted"))
        elif mic_signal is None:
            c.append(Check("mic", "Mic is on and has signal", WARN, "No level data yet"))
        elif not mic_signal:
            c.append(Check("mic", "Mic is on and has signal", WARN, "Mic is on but quiet, say something"))
        else:
            c.append(Check("mic", "Mic is on and has signal", OK, mic_name))
        c.append(Check("camera", "Camera is plugged in", OK if camera_present else WARN,
                       "" if camera_present else "Camera device not found"))
        c.append(_preset_check(camera_present, camera_preset))
        c.append(_light_check(camera_light))
        if stream_key_set is None:
            c.append(Check("key", "Stream key is set in OBS", WARN, "Could not read OBS stream settings"))
        else:
            c.append(Check("key", "Stream key is set in OBS", OK if stream_key_set else FAIL,
                           "" if stream_key_set else "Add your stream key in OBS, Settings, Stream"))
    if twitch_connected:
        c.append(Check("title", "Twitch title is set", OK if twitch_title.strip() else WARN,
                       twitch_title.strip() or "Your stream has no title"))
    else:
        c.append(Check("title", "Twitch title is set", SKIP, "Connect Twitch to check this"))
    return c


def _preset_check(camera_present, camera_preset):
    label = "Camera preset is loaded"
    if not camera_present or camera_preset is None:
        return Check("preset", label, SKIP, "No camera to check" if not camera_present else "Camera presets not readable")
    num, loaded = camera_preset
    if loaded:
        return Check("preset", label, OK, f"Preset {num}")
    return Check("preset", label, WARN, f"Preset {num} is not loaded")


def _light_check(camera_light):
    label = "Camera image looks dark"
    if camera_light is None:
        return Check("light", label, SKIP, "No camera image from OBS yet")
    dark, luma = camera_light
    if dark:
        return Check("light", label, WARN, f"Brightness {luma:.0f} of 255, normally around 100")
    return Check("light", label, OK, f"Brightness {luma:.0f} of 255")


def can_go_live(checks):
    return not any(x.status == FAIL for x in checks)
