import asyncio
import unittest

from lark.obsws import ObsClient, ObsError, make_auth
from tests.fake_obs import FakeObs


class Harness:
    def __init__(self, password="pw", client_password=None, **kw):
        self.fake = FakeObs(password=password)
        self.client_password = password if client_password is None else client_password
        self.events, self.states = [], []
        self.kw = kw

    async def __aenter__(self):
        self.port = await self.fake.start()
        self.client = ObsClient(lambda: ("127.0.0.1", self.port, self.client_password),
                                lambda t, d: self.events.append((t, d)),
                                lambda s, d: self.states.append(s),
                                backoff=(0.05, 0.05, 0.1), **self.kw)
        self.task = asyncio.ensure_future(self.client.run())
        return self

    async def wait_state(self, state, timeout=3):
        async def w():
            while self.client.state != state:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(w(), timeout)

    async def __aexit__(self, *a):
        await self.client.stop()
        await asyncio.wait_for(self.task, 3)
        await self.fake.stop()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


class AuthTests(unittest.TestCase):
    def test_make_auth_known_value(self):
        import base64, hashlib
        secret = base64.b64encode(hashlib.sha256(b"passwordsalt").digest())
        want = base64.b64encode(hashlib.sha256(secret + b"challenge").digest()).decode()
        self.assertEqual(make_auth("password", "salt", "challenge"), want)


class ClientTests(unittest.TestCase):
    def test_connect_and_scene_requests(self):
        async def go():
            async with Harness() as h:
                await h.wait_state("connected")
                r = await h.client.request("GetSceneList")
                self.assertEqual(r["currentProgramSceneName"], "Game")
                await h.client.request("SetCurrentProgramScene", {"sceneName": "BRB"})
                await asyncio.sleep(0.1)
                self.assertIn(("CurrentProgramSceneChanged", {"sceneName": "BRB"}), h.events)
                items = (await h.client.request("GetSceneItemList", {"sceneName": "Game"}))["sceneItems"]
                self.assertEqual([i["sourceName"] for i in items], ["Game capture", "Camera"])
                await h.client.request("SetSceneItemEnabled", {"sceneName": "Game", "sceneItemId": items[1]["sceneItemId"],
                                                               "sceneItemEnabled": False})
                self.assertFalse(h.fake.scenes["Game"][1][1])
        run(go())

    def test_error_response_raises_with_code(self):
        async def go():
            async with Harness() as h:
                await h.wait_state("connected")
                with self.assertRaises(ObsError) as cm:
                    await h.client.request("SetCurrentProgramScene", {"sceneName": "Nope"})
                self.assertEqual(cm.exception.code, 600)
        run(go())

    def test_outputs_and_audio(self):
        async def go():
            async with Harness() as h:
                await h.wait_state("connected")
                c = h.client
                await c.request("StartStream")
                self.assertTrue((await c.request("GetStreamStatus"))["outputActive"])
                await c.request("StopStream")
                await c.request("StartRecord"); await c.request("StopRecord")
                await c.request("StartReplayBuffer"); await c.request("SaveReplayBuffer")
                await c.request("StartVirtualCam"); await c.request("StopVirtualCam")
                self.assertIn("cpuUsage", await c.request("GetStats"))
                await c.request("SetInputVolume", {"inputName": "Mic (Fifine)", "inputVolumeDb": -20.5})
                self.assertEqual((await c.request("GetInputVolume", {"inputName": "Mic (Fifine)"}))["inputVolumeDb"], -20.5)
                await c.request("SetInputMute", {"inputName": "Mic (Fifine)", "inputMuted": True})
                self.assertTrue((await c.request("GetInputMute", {"inputName": "Mic (Fifine)"}))["inputMuted"])
                names = [i["inputName"] for i in (await c.request("GetInputList"))["inputs"]]
                self.assertIn("Desktop Audio", names)
                kinds = {t for t, _ in h.events}
                self.assertTrue({"StreamStateChanged", "RecordStateChanged", "ReplayBufferSaved",
                                 "VirtualcamStateChanged", "InputMuteStateChanged"} <= kinds)
        run(go())

    def test_meter_events_delivered_when_subscribed(self):
        async def go():
            async with Harness() as h:
                h.fake.send_meters = True
                await h.wait_state("connected")
                await asyncio.sleep(0.4)
                self.assertTrue(any(t == "InputVolumeMeters" for t, _ in h.events))
        run(go())

    def test_wrong_password_reports_auth_failed_and_keeps_trying(self):
        async def go():
            async with Harness(client_password="wrong") as h:
                await h.wait_state("auth_failed")
                await asyncio.sleep(0.4)
                self.assertGreaterEqual(h.fake.auth_failures, 2)
        run(go())

    def test_no_auth_server(self):
        async def go():
            async with Harness(password=None, client_password="") as h:
                await h.wait_state("connected")
        run(go())

    def test_reconnects_after_drop_and_pending_fail(self):
        async def go():
            async with Harness() as h:
                await h.wait_state("connected")
                await h.fake.drop_clients()
                await h.wait_state("disconnected")
                with self.assertRaises(ObsError):
                    await h.client.request("GetStats")
                await h.wait_state("connected")
                self.assertEqual(h.fake.connections, 2)
                self.assertIn("cpuUsage", await h.client.request("GetStats"))
        run(go())

    def test_server_down_then_up(self):
        async def go():
            fake = FakeObs()
            port = await fake.start()
            await fake.stop()
            states = []
            client = ObsClient(lambda: ("127.0.0.1", port, "pw"), None, lambda s, d: states.append(s),
                               backoff=(0.05,))
            task = asyncio.ensure_future(client.run())
            await asyncio.sleep(0.3)
            self.assertNotIn("connected", states)
            fake2 = FakeObs()
            await fake2.start(port)
            for _ in range(100):
                if client.connected:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(client.connected)
            await client.stop(); await asyncio.wait_for(task, 3); await fake2.stop()
        run(go())

    def test_request_timeout(self):
        async def go():
            async with Harness(request_timeout=0.2) as h:
                await h.wait_state("connected")
                orig = h.fake.handle_request
                async def slow(t, d):
                    await asyncio.sleep(1)
                    return await orig(t, d)
                h.fake.handle_request = slow
                with self.assertRaises(ObsError):
                    await h.client.request("GetStats")
        run(go())

    def test_request_when_disconnected_raises(self):
        async def go():
            c = ObsClient(lambda: ("127.0.0.1", 1, ""))
            with self.assertRaises(ObsError):
                await c.request("GetStats")
        run(go())


if __name__ == "__main__":
    unittest.main()
