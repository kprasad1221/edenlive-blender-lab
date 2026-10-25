"""Deliberate, bounded paid smoke test. Never run by CI.

Requires EDEN_ALLOW_PAID=I_ACCEPT_REACTOR_CHARGES, REACTOR_API_KEY and online access.
Renders 48 demo frames locally before connecting. Default paid limit: 30 seconds.
See docs/PAID_TEST.md; the interactive Blender workflow is recommended first.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

import bpy

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eden_live_previs import backends, capture, deps, demo
from eden_live_previs.recorder import Recorder
from eden_live_previs.safety import paid_limit
from eden_live_previs.session import EdenSession


def main():
    if os.environ.get("EDEN_ALLOW_PAID") != "I_ACCEPT_REACTOR_CHARGES":
        print("SKIPPED: explicit paid-test opt-in is required. No session opened.")
        return 0
    if os.environ.get("EDEN_LIVE_MOCK", "").lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("Offline override is active; refusing to run a paid test.")
    key = os.environ.get("REACTOR_API_KEY", "").strip()
    if not key or not bpy.app.online_access:
        raise RuntimeError("An API key and Blender online access are required.")
    limit = paid_limit(int(os.environ.get("EDEN_PAID_SECONDS", "30")))
    ok, _ = deps.sdk_status()
    if not ok:
        raise RuntimeError("Install the bundled extension or its pinned Reactor SDK first.")
    out = pathlib.Path(os.environ.get("EDEN_TEST_OUT") or tempfile.mkdtemp(prefix="edenlive-paid-"))
    out.mkdir(parents=True, exist_ok=True)
    session = EdenSession()
    session.add_secret(key)
    rec = None
    try:
        scene = demo.build_demo_scene(bpy.context.scene)
        scene.frame_start, scene.frame_end = 1, 48
        ref = os.environ.get("EDEN_REFERENCE") or str(out / "reference.png")
        if not os.environ.get("EDEN_REFERENCE"):
            capture.save_bgra_png(capture.render_capture(scene), ref)
        if not pathlib.Path(ref).is_file():
            raise RuntimeError("Reference not found; no session opened.")
        settings = {
            "frames": range(1, 49), "stream_fps": 24.0, "output_fps": 24.0,
            "out_dir": str(out), "name": "paid_test", "tail_seconds": 3.0,
            "reference_path": ref, "prompt": demo.DEMO_PROMPT, "seed": 1,
            "reference_pixels": None, "keep_frames": True, "warmup_timeout": 15.0,
            "connect_factory": lambda cb: backends.ReactorBackend(cb, key, max_session_seconds=limit),
            "connect_label": backends.ReactorBackend.label,
        }
        rec = Recorder(session, settings)
        for frame in rec.frames:
            scene.frame_set(frame)
            rec.add_frame(capture.render_capture(scene))
        print(f"Drafts ready. Opening one paid attempt, limit {limit}s. No automatic retry.")
        rec.begin_stream()
        deadline = time.monotonic() + limit + 120
        while not rec.finished:
            rec.poll()
            if time.monotonic() >= deadline:
                raise RuntimeError("Test timed out")
            time.sleep(0.05)
        if rec.phase != "DONE":
            raise RuntimeError(rec.message)
        print("PASS: recorded. Check the Reactor dashboard for final session state and usage.")
        return 0
    except Exception as exc:
        print("FAIL: " + session.redact(str(exc)))
        return 1
    finally:
        if rec is not None and not rec.finished:
            rec.cancel()
        session.shutdown()
        (out / "debug.json").write_text(json.dumps(session.debug_report(), indent=2), encoding="utf-8")


try:
    code = main()
except Exception as exc:
    print(type(exc).__name__ + ": " + str(exc))
    code = 1
sys.stdout.flush()
os._exit(code)
