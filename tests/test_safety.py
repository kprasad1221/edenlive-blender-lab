"""Fast lifecycle tests with fake transports. No Blender, API key, or paid connection."""

import asyncio
import importlib
import json
import pathlib
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

# Load the bpy-free modules without executing Blender's add-on registration module.
ROOT = pathlib.Path(__file__).resolve().parents[1]
package = types.ModuleType("_eden_lab_test")
package.__path__ = [str(ROOT / "eden_live_previs")]
sys.modules[package.__name__] = package
sessions = importlib.import_module("_eden_lab_test.session")
backends = importlib.import_module("_eden_lab_test.backends")
safety = importlib.import_module("_eden_lab_test.safety")
diagnostics = importlib.import_module("_eden_lab_test.diagnostics")


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("condition timed out")
        time.sleep(0.01)


class FakePaidBackend:
    is_paid = True
    max_session_seconds = 0.3  # Accelerated fake clock budget; never passed to the SDK.

    def __init__(self, callback, delay=0):
        self.callback = callback
        self.delay = delay
        self.closed = 0
        self.commands = []
        self.close_error = False
        self.command_error = False

    async def connect(self):
        await asyncio.sleep(self.delay)

    async def disconnect(self):
        self.closed += 1
        if self.close_error:
            raise RuntimeError("fake close failed")

    async def send_command(self, name, data):
        self.commands.append(name)
        if self.command_error:
            raise RuntimeError("fake command failed")
        return None

    def push_frame(self, *args):
        pass


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.session = sessions.EdenSession()

    def tearDown(self):
        if self.session.backend:
            self.session.backend.close_error = False
        self.session.shutdown()

    def connect(self, **kwargs):
        self.backend = FakePaidBackend(self.session, **kwargs)
        self.session.connect(lambda cb: self.backend, "Fake paid transport")
        return self.backend

    def test_budget_closes_idle_session(self):
        backend = self.connect()
        wait_for(lambda: backend.closed == 1)
        wait_for(lambda: self.session.status == "disconnected")
        self.assertIn("time limit", self.session.stop_reason)

    def test_budget_keeps_counting_while_paused(self):
        backend = self.connect()
        wait_for(lambda: self.session.ready)
        self.session.state = {"paused": True, "started": True}
        wait_for(lambda: backend.closed == 1)

    def test_stop_during_connect_does_not_become_ready_later(self):
        backend = self.connect(delay=1)
        wait_for(lambda: self.session.backend is not None)
        self.session.disconnect().result(timeout=2)
        time.sleep(0.1)
        self.assertEqual(self.session.status, "disconnected")
        self.assertEqual(backend.commands, [])
        self.assertEqual(backend.closed, 1)

    def test_connect_timeout_cleans_up(self):
        with patch.object(sessions, "CONNECT_TIMEOUT", 0.05):
            backend = self.connect(delay=1)
            wait_for(lambda: backend.closed == 1)
            wait_for(lambda: bool(self.session.last_error))
            self.assertIn("TimeoutError", self.session.last_error)

    def test_failed_command_closes_paid_session(self):
        backend = self.connect()
        wait_for(lambda: self.session.ready)
        backend.command_error = True
        with self.assertRaises(RuntimeError):
            self.session.set_prompt("private test prompt").result(timeout=2)
        self.assertEqual(backend.closed, 1)

    def test_failed_close_is_visible_and_retryable(self):
        backend = self.connect()
        wait_for(lambda: self.session.ready)
        backend.close_error = True
        self.assertFalse(self.session.disconnect().result(timeout=2))
        self.assertEqual(self.session.close_result, "unconfirmed")
        self.assertIs(self.session.backend, backend)
        with self.assertRaises(sessions.UserError):
            self.session.connect(lambda cb: FakePaidBackend(cb), "Reactor")
        backend.close_error = False
        self.assertTrue(self.session.disconnect().result(timeout=2))
        self.assertEqual(backend.closed, 2)

    def test_first_frame_timeout_closes(self):
        with patch.object(sessions, "FIRST_FRAME_TIMEOUT", 0.02):
            backend = self.connect()
            wait_for(lambda: self.session.ready)
            self.session._world_started_at = time.monotonic()
            self.session.state = {"paused": False}
            wait_for(lambda: backend.closed == 1)
            self.assertIn("first output", self.session.stop_reason)

    def test_output_stall_closes(self):
        with patch.object(sessions, "OUTPUT_STALL_TIMEOUT", 0.02):
            backend = self.connect()
            wait_for(lambda: self.session.ready)
            self.session._world_started_at = time.monotonic() - 0.02
            self.session._last_output_at = time.monotonic()
            self.session.state = {"paused": False}
            wait_for(lambda: backend.closed == 1)
            self.assertIn("stalled", self.session.stop_reason)

    def test_repeated_stop_is_idempotent(self):
        backend = self.connect()
        wait_for(lambda: self.session.ready)
        a = self.session.disconnect()
        b = self.session.disconnect()
        a.result(timeout=2)
        b.result(timeout=2)
        self.assertEqual(backend.closed, 1)

    def test_sdk_constructor_receives_server_limit(self):
        seen = {}
        class Track:
            def on_raw_frame(self, handler): pass
            async def publish(self): return self
            async def set_bitrate(self, **kwargs): pass
        class Reactor:
            def __init__(self, **kwargs): seen.update(kwargs)
            def on(self, *args): pass
            def track(self, *args): return Track()
            async def connect(self): pass
            async def disconnect(self): pass
            async def set_bitrate(self, **kwargs): pass
            def close(self): pass
        with patch.dict(sys.modules, {"reactor_sdk": types.SimpleNamespace(Reactor=Reactor)}):
            backend = backends.ReactorBackend(self.session, "rk_fake-test", max_session_seconds=30)
            asyncio.run(backend.connect())
            asyncio.run(backend.disconnect())
        self.assertEqual(seen["max_session_duration_seconds"], 30)
        self.assertEqual(seen["model_name"], "reactor/eden-live")

    def test_budget_cannot_be_disabled(self):
        for value in (None, 0, -1, 121, 3600, True, 30.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                backends.ReactorBackend(None, "fake", max_session_seconds=value)

    def test_diagnostics_redact_keys_tokens_paths_and_prompts(self):
        d = diagnostics.Diagnostics()
        d.protect("a private prompt")
        d.event("test", 'rk_fakeKEY123 a private prompt Bearer secret https://host.test/upload?token=secret '
                       'C:\\Users\\Person\\scene.blend File "/home/person/code.py"')
        result = json.dumps(d.snapshot())
        for private in ("fakeKEY123", "a private prompt", "secret", "Person", "person", "host.test"):
            self.assertNotIn(private, result)

    def test_offline_backend_has_no_network_or_sdk_dependency(self):
        async def exercise():
            # Create asyncio's Windows loopback wakeup pipe before blocking sockets.
            # The backend itself must still perform zero network operations.
            with patch("socket.socket.connect", side_effect=AssertionError("Network attempted")), \
                 patch.dict(sys.modules, {"reactor_sdk": None}):
                backend = backends.MockBackend(self.session)
                await backend.connect()
                await backend.send_command("set_source", {"source": "client"})
                await backend.disconnect()
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
