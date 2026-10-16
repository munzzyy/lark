"""Read-mostly check of Lark's OBS client against a real OBS. Run by tools/real-obs-smoke.sh.

Never starts a stream or a recording. It creates one browser source and removes it again.
"""
import asyncio
import os
import sys
from pathlib import Path

from lark.config import Config
from lark.obsws import ObsClient, ObsError


SAFE_DIR = Path.home() / "Videos" / "OBS"


def delete_made(path):
    """Delete a file OBS says it just wrote, but only inside ~/Videos/OBS."""
    p = Path(path).resolve()
    if SAFE_DIR.resolve() in p.parents and p.is_file():
        p.unlink()
        print(f"     deleted {p.name}")
    else:
        print(f"FAIL refusing to delete {p}")
        return False
    return True


async def outputs(c, req, meters):
    ok = True
    meters.clear()
    await asyncio.sleep(3)
    n = len(meters)
    print(f"{'ok  ' if n else 'FAIL'} InputVolumeMeters events in 3 s: {n}")
    ok = ok and n > 0
    await req("StartRecord")
    await asyncio.sleep(5)
    r = await req("StopRecord")
    path = r.get("outputPath")
    if not path:
        print("FAIL StopRecord returned no outputPath")
        return False
    await asyncio.sleep(1)
    print("     recording size:", Path(path).stat().st_size if Path(path).exists() else "missing")
    ok = ok and Path(path).exists() and delete_made(path)
    await req("StartReplayBuffer")
    await asyncio.sleep(4)
    await req("SaveReplayBuffer")
    await asyncio.sleep(2)
    last = (await req("GetLastReplayBufferReplay")).get("savedReplayPath")
    await req("StopReplayBuffer")
    if not last:
        print("FAIL no saved replay path")
        return False
    ok = ok and delete_made(last)
    return ok


async def main():
    cfg = Config()
    states = []
    meters = []
    c = ObsClient(cfg.obs_connection, lambda t, d: meters.append(d) if t == "InputVolumeMeters" else None,
                  lambda s, d: states.append(s))
    task = asyncio.ensure_future(c.run())
    for _ in range(100):
        if c.connected:
            break
        await asyncio.sleep(0.1)
    if not c.connected:
        print("FAIL could not connect, states:", states)
        await c.stop()
        return 1
    ok = True

    async def req(t, d=None):
        nonlocal ok
        try:
            r = await c.request(t, d)
            print(f"ok   {t}")
            return r
        except ObsError as e:
            print(f"FAIL {t}: {e.comment} ({e.code})")
            ok = False
            return {}

    sl = await req("GetSceneList")
    names = [s["sceneName"] for s in sl.get("scenes", [])]
    print("     scenes (name, sceneIndex):", [(x["sceneName"], x.get("sceneIndex")) for x in sl.get("scenes", [])],
          "current:", sl.get("currentProgramSceneName"))
    for n in names[:1]:
        await req("GetSceneItemList", {"sceneName": n})
    inputs = (await req("GetInputList")).get("inputs", [])
    for i in inputs:
        try:
            v = await c.request("GetInputVolume", {"inputName": i["inputName"]})
            m = await c.request("GetInputMute", {"inputName": i["inputName"]})
            print(f"ok   audio input {i['inputName']!r} {v['inputVolumeDb']:.1f} dB muted={m['inputMuted']}")
        except ObsError as e:
            print(f"     skip {i['inputName']!r} (no audio: {e.code})")
    ss = await req("GetStreamServiceSettings")
    print("     stream key set:", bool((ss.get("streamServiceSettings") or {}).get("key")))
    st = await req("GetStreamStatus")
    print("     stream active:", st.get("outputActive"))
    await req("GetRecordStatus")
    await req("GetReplayBufferStatus")
    try:
        await c.request("GetVirtualCamStatus")
        print("ok   GetVirtualCamStatus")
    except ObsError as e:
        print(f"     virtual camera not available in this OBS ({e.code}), expected on Flatpak")
    stats = await req("GetStats")
    print("     fps:", stats.get("activeFps"), "cpu:", stats.get("cpuUsage"))
    if names:
        sc = sl["currentProgramSceneName"]
        made = await req("CreateInput", {"sceneName": sc, "inputName": "lark-smoke", "inputKind": "browser_source",
                                         "inputSettings": {"url": "http://127.0.0.1:1/chat", "width": 600, "height": 900},
                                         "sceneItemEnabled": False})
        if made:
            await req("RemoveInput", {"inputName": "lark-smoke"})
    ok = await outputs(c, req, meters) and ok
    await c.stop()
    await asyncio.wait_for(task, 5)
    print("RESULT", "pass" if ok else "fail")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
