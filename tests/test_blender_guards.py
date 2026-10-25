"""Blender integration guards; deliberately does not connect to Reactor or render."""
import json
import os
import pathlib
import sys
import tempfile
import time
from unittest.mock import patch

import bpy

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import eden_live_previs as addon
from eden_live_previs import operators, prefs
from eden_live_previs.session import get_session
from eden_live_previs.backends import MockBackend
from eden_live_previs.recorder import Recorder
import numpy as np

addon.register()
try:
    os.environ.pop("EDEN_LIVE_MOCK", None)
    runtime = bpy.context.window_manager.eden_live_runtime
    assert prefs.use_mock(), "default must be offline"
    runtime.backend_mode = "PAID"
    try:
        operators.backend_factory(bpy.context)
    except Exception as exc:
        assert "confirmation" in str(exc)
    else:
        raise AssertionError("Paid factory accepted no confirmation")
    runtime.backend_mode = "MOCK"
    with patch.object(operators.ReactorBackend, "connect", side_effect=AssertionError("Paid connect attempted")):
        bpy.ops.eden_live.check_inputs()
        assert get_session().backend is None
        report = pathlib.Path(tempfile.mkdtemp()) / "debug.json"
        bpy.ops.eden_live.export_debug(filepath=str(report))
        assert json.loads(report.read_text())["status"] == "disconnected"
    # Synthetic draft frames verify that rendering/preparation cannot start a session.
    session = get_session()
    created = []
    def factory(cb):
        created.append(True)
        return MockBackend(cb, latency=0.01)
    out = pathlib.Path(tempfile.mkdtemp())
    ref = out / "ref.png"
    from eden_live_previs import imaging
    ref.write_bytes(imaging.encode_png(np.zeros((32, 32, 3), dtype=np.uint8)))
    settings = dict(frames=range(2), stream_fps=24, output_fps=24, out_dir=str(out),
                    name="guard", tail_seconds=1, reference_path=str(ref), prompt="test", seed=1,
                    keep_frames=True, connect_factory=factory, connect_label=MockBackend.label)
    rec = Recorder(session, settings)
    rec.add_frame(np.zeros((32, 32, 4), dtype=np.uint8))
    rec.add_frame(np.zeros((32, 32, 4), dtype=np.uint8))
    assert created == [] and session.backend is None, "Capture must be free"
    rec.begin_stream()
    deadline = time.monotonic() + 3
    while not session.ready and time.monotonic() < deadline:
        time.sleep(0.01)
    assert created == [True] and session.ready
    rec.cancel()
    deadline = time.monotonic() + 3
    while session.status != "disconnected" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status == "disconnected", "Cancel must close the session"
    runtime.backend_mode = "PAID"
    addon._on_load()
    assert runtime.backend_mode == "MOCK"
    assert set(runtime.bl_rna.properties.keys()) >= {"paid_seconds", "backend_mode"}
    print("PASS Blender guards: offline default, paid gate, free preflight, debug export, deferred connection, cancel cleanup, load reset")
finally:
    addon.unregister()
