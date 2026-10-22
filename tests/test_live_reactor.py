"""End-to-end test against the real reactor/eden-live model (billed session).

    REACTOR_API_KEY=rk_... blender -b --factory-startup --python tests/test_live_reactor.py

Steps: connect, read the model's schema and check the tracks/commands this add-on
relies on, render the demo scene's first frame as a reference, record the first
EDEN_FRAMES (default 48) timeline frames through EdenLive, write MP4s + report to
EDEN_TEST_OUT, disconnect. Optionally set EDEN_REFERENCE to a restyled reference
image for nicer output. Prints SKIPPED and exits 0 when no key is set.
"""

import json
import os
import sys
import tempfile
import time
import traceback

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from eden_live_previs import backends, capture, deps, demo  # noqa: E402
from eden_live_previs.recorder import Recorder  # noqa: E402
from eden_live_previs.session import EdenSession  # noqa: E402

OUT = os.environ.get("EDEN_TEST_OUT") or os.path.join(tempfile.gettempdir(), "eden_live_reactor_test")
FRAMES = int(os.environ.get("EDEN_FRAMES", "48"))


def say(msg):
    print(msg)
    sys.stdout.flush()


def wait(pred, timeout, what):
    deadline = time.perf_counter() + timeout
    while not pred():
        if time.perf_counter() > deadline:
            raise AssertionError(f"timeout waiting for {what}")
        time.sleep(0.05)


def main():
    key = os.environ.get("REACTOR_API_KEY", "").strip()
    if not key:
        say("SKIPPED: set REACTOR_API_KEY to run the live EdenLive test")
        return 0
    deps.add_deps_path()
    ok, detail = deps.sdk_status()
    if not ok:
        say(f"FAIL: reactor-sdk not importable ({detail}); pip install reactor-sdk into Blender's Python")
        return 1
    say(f"Using {detail}")
    os.makedirs(OUT, exist_ok=True)

    session = EdenSession()
    session.add_secret(key)
    session.fps = 24
    session.connect(lambda cb: backends.ReactorBackend(cb, key, max_session_seconds=900), "Reactor")
    try:
        wait(lambda: session.status in ("ready", "error"), 300, "connect (GPU assignment can take a while)")
        assert session.ready, session.last_error
        say(f"PASS connected, session {getattr(session.backend._reactor, 'session_id', '?')}")

        schema = session.submit(session.backend._reactor.request_schema()).result(timeout=30)
        with open(os.path.join(OUT, "schema.json"), "w") as f:
            json.dump(schema, f, indent=2)
        tracks = {t.name: (str(t.kind), str(t.direction)) for t in session.backend._reactor.tracks}
        say(f"tracks: {tracks}")
        for name in ("source_video", "main_video", "draft_video"):
            assert name in tracks, f"missing track {name}"
        text = json.dumps(schema)
        for command in ("set_source", "set_image", "set_prompt", "start", "set_paused"):
            assert command in text, f"schema does not mention {command}"
        say(f"PASS schema has the expected tracks and commands; state {session.state}")

        scene = demo.build_demo_scene(bpy.context.scene)
        scene.frame_start, scene.frame_end = 1, FRAMES
        reference = os.environ.get("EDEN_REFERENCE") or os.path.join(OUT, "reference.png")
        if not os.environ.get("EDEN_REFERENCE"):
            scene.frame_set(1)
            capture.save_bgra_png(capture.render_capture(scene), reference)
        settings = {
            "frames": range(1, FRAMES + 1),
            "stream_fps": 24.0,
            "output_fps": 24.0,
            "out_dir": OUT,
            "name": "eden_live_take",
            "tail_seconds": 3.0,
            "reference_path": reference,
            "prompt": demo.DEMO_PROMPT,
            "seed": 2026092901,
            "reference_pixels": None,
            "encoder": "AUTO",
            "keep_frames": True,
            "warmup_timeout": 120.0,
        }
        rec = Recorder(session, settings)
        for f in rec.frames:
            scene.frame_set(f)
            rec.add_frame(capture.render_capture(scene))
        say(f"rendered {FRAMES} draft frames; streaming")
        rec.begin_stream()
        deadline = time.perf_counter() + 600
        last = None
        while not rec.finished:
            rec.poll()
            if rec.message != last:
                last = rec.message
                say(f"  {rec.phase}: {rec.message} | state chunks={session.state.get('chunks')} "
                    f"seconds={session.state.get('seconds')}")
            time.sleep(0.05)
            assert time.perf_counter() < deadline, f"stuck in {rec.phase}"
        assert rec.phase == "DONE", rec.message
        say(f"PASS recorded: {json.dumps(rec.outputs, indent=2)}")
        say(f"report: {json.dumps(rec.report, indent=2)}")
        return 0
    except Exception:
        say("FAIL\n" + traceback.format_exc())
        say(f"log:\n" + "\n".join(session.log))
        return 1
    finally:
        session.shutdown()
        say("disconnected")


code = main()
sys.stdout.flush()
os._exit(code)
