"""Headless test suite. Run with:

    blender -b --factory-startup --python tests/test_headless.py

Covers: add-on registration, the settings surface, API URL handling, pixel helpers, frame alignment, the demo scene,
render-engine frame capture, a mock EdenLive session, and the full
record -> align -> MP4 pipeline with both encoders. Exits non-zero on failure.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import eden_live_previs as addon  # noqa: E402
from eden_live_previs import align, backends, capture, demo, encode, imaging  # noqa: E402
from eden_live_previs.recorder import Recorder  # noqa: E402
from eden_live_previs.session import EdenSession  # noqa: E402

OUT = os.environ.get("EDEN_TEST_OUT") or os.path.join(tempfile.gettempdir(), "eden_live_tests")
RESULTS = []


def check(name):
    def wrap(fn):
        def run():
            t = time.perf_counter()
            try:
                fn()
                RESULTS.append((name, True, time.perf_counter() - t, ""))
                print(f"PASS {name} ({time.perf_counter() - t:.1f}s)")
            except Exception:
                RESULTS.append((name, False, time.perf_counter() - t, traceback.format_exc()))
                print(f"FAIL {name}\n{traceback.format_exc()}")

        return run

    return wrap


def ffprobe(path):
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,nb_read_frames,codec_name", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["streams"][0]


@check("register add-on")
def test_register():
    addon.register()
    assert hasattr(bpy.types.Scene, "eden_live")
    assert hasattr(bpy.ops.eden_live, "record")
    assert bpy.context.scene.eden_live.fps == 20


@check("imaging: png round trip, resize, thumbnails")
def test_imaging():
    rng = np.random.default_rng(0)
    rgb = rng.integers(0, 255, (37, 53, 3), dtype=np.uint8)
    assert np.array_equal(imaging.decode_png_rgb(imaging.encode_png(rgb)), rgb)
    bgra = imaging.rgb_to_bgra(rgb)
    assert np.array_equal(imaging.bgra_to_rgb(bgra), rgb)
    big = rng.integers(0, 255, (1080, 1920, 4), dtype=np.uint8)
    assert imaging.cover_resize(big, 1280, 704).shape == (704, 1280, 4)
    px = imaging.bgra_to_image_pixels(bgra)
    back = imaging.image_pixels_to_bgra(px, 53, 37)
    assert np.array_equal(back, bgra)
    assert imaging.thumbnail(big).shape == (imaging.THUMB_H, imaging.THUMB_W)


@check("align: tags, draft matching, static stretches")
def test_align():
    rng = np.random.default_rng(1)
    n, fps, latency = 60, 24.0, 1.1
    thumbs = rng.random((n, imaging.THUMB_H, imaging.THUMB_W)).astype(np.float32)
    thumbs[20:35] = thumbs[20]  # a static stretch: identical drafts
    sent = 100.0 + np.arange(n) / fps
    received = []
    truth = []
    for i in range(n):
        noisy = thumbs[i] + rng.normal(0, 0.01, thumbs[i].shape).astype(np.float32)
        received.append({"thumb": noisy, "arrival": sent[i] + latency + rng.normal(0, 0.01)})
        truth.append(i)
    idx, info = align.align(thumbs, sent, received, fps)
    err = np.abs(idx - np.array(truth))
    assert abs(info["latency"] - latency) < 0.05, info
    assert (err <= 1).mean() > 0.95, err
    tagged = [dict(r, tag_index=t) for r, t in zip(received, truth)]
    idx2, info2 = align.align(thumbs, sent, tagged, fps)
    assert np.array_equal(idx2, np.array(truth)) and info2["tagged"] == n
    choice, coverage = align.pick_frames(idx, n)
    assert len(choice) == n and coverage > 0.9

    # Slow pan: neighbouring drafts differ by about as much as the stream noise.
    xs = np.linspace(0, 6, imaging.THUMB_W)
    pan = np.stack([np.sin(xs[None, :] + 0.02 * i + np.linspace(0, 2, imaging.THUMB_H)[:, None]) for i in range(n)])
    pan = (pan * 0.5 + 0.5).astype(np.float32)
    noisy = [{"thumb": pan[i] + rng.normal(0, 0.01, pan[i].shape), "arrival": sent[i] + latency} for i in range(n)]
    idx3, _ = align.align(pan, sent, noisy, fps)
    assert (np.abs(idx3 - np.arange(n)) <= 1).mean() > 0.9, idx3

    # Realistic stream: hold frame 0, eased-in start (near-identical drafts), jittered
    # arrivals, then the last frame held for the tail.
    phase = 3.0 * (np.arange(n) / (n - 1)) ** 2
    eased = np.stack([np.sin(xs[None, :] + p + np.linspace(0, 2, imaging.THUMB_H)[:, None]) for p in phase])
    eased = (eased * 0.5 + 0.5).astype(np.float32)
    hold, tail = 12, 24
    order = [0] * hold + list(range(n)) + [n - 1] * tail
    push = 100.0 + (np.arange(len(order)) - hold) / fps
    stream = [
        {"thumb": eased[i] + rng.normal(0, 0.005, eased[i].shape), "arrival": t + latency + rng.normal(0, 0.04)}
        for i, t in zip(order, push)
    ]
    idx4, info4 = align.align(eased, sent, stream, fps)
    err = np.abs(idx4 - np.array(order))
    assert (err <= 1).mean() > 0.95, (err, info4)
    _, coverage4 = align.pick_frames(idx4, n)
    assert coverage4 > 0.9, coverage4


@check("demo scene + render-engine capture")
def test_demo_capture():
    scene = demo.build_demo_scene(bpy.context.scene)
    assert scene.camera is not None and len(scene.objects) > 30
    frame = capture.render_capture(scene)
    assert frame.shape == (704, 1280, 4)
    colors = frame[..., :3].reshape(-1, 3)
    assert colors.std(axis=0).mean() > 20, "capture looks blank"
    os.makedirs(OUT, exist_ok=True)
    capture.save_bgra_png(frame, os.path.join(OUT, "demo_draft_frame1.png"))
    capture.save_bgra_png(frame, os.path.join(OUT, "reference.png"))


def _wait(pred, timeout, what):
    deadline = time.perf_counter() + timeout
    while not pred():
        if time.perf_counter() > deadline:
            raise AssertionError(f"timeout waiting for {what}")
        time.sleep(0.02)


@check("mock session: connect, refusals, start, stream, prompt, pause")
def test_mock_session():
    session = EdenSession()
    session.fps = 24
    session.connect(lambda cb: backends.MockBackend(cb, latency=0.5), backends.MockBackend.label)
    _wait(lambda: session.ready, 10, "connect")
    assert session.state.get("source") == "client"
    frame = capture.load_image_bgra(os.path.join(OUT, "reference.png"))
    session.push_source_frame(frame)
    session.start_world("/nonexistent.png", "x", 1).exception(timeout=5)
    assert "reference" in session.last_error.lower()
    session.start_world(os.path.join(OUT, "reference.png"), "A temple", 7, frame[::8, ::8]).result(timeout=10)
    assert session.state["started"] and not session.state["paused"] and session.state["seed"] == 7
    _wait(lambda: session.main.seq > 10 and session.draft.seq > 10, 10, "output frames")
    main, _ = session.main.get()
    assert main.shape == (704, 1280, 4)
    _wait(lambda: session.take_preview(0) is not None, 5, "preview")
    session.set_prompt("A temple at night").result(timeout=5)
    assert session.state["prompt"] == "A temple at night"
    session.set_paused(True).result(timeout=5)
    assert session.state["paused"]
    session.disconnect().result(timeout=5)
    assert session.status == "disconnected"
    session.shutdown()


def _run_recording(name, encoder, echo_tags, n_frames, keep_frames=True):
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, n_frames
    session = EdenSession()
    session.fps = 24
    session.connect(lambda cb: backends.MockBackend(cb, latency=1.0, echo_tags=echo_tags), backends.MockBackend.label)
    ref = os.path.join(OUT, "reference.png")
    settings = {
        "frames": range(1, n_frames + 1),
        "stream_fps": 24.0,
        "output_fps": 24.0,
        "out_dir": OUT,
        "name": name,
        "tail_seconds": 2.0,
        "reference_path": ref,
        "prompt": demo.DEMO_PROMPT,
        "seed": 3,
        "reference_pixels": capture.load_image_bgra(ref, max_side=256),
        "encoder": encoder,
        "keep_frames": keep_frames,
        "warmup_timeout": 30.0,
    }
    rec = Recorder(session, settings)
    t0 = time.perf_counter()
    for f in rec.frames:
        scene.frame_set(f)
        done = rec.add_frame(capture.render_capture(scene))
    assert done
    capture_s = time.perf_counter() - t0
    drafts = np.array(rec.drafts)
    rec.begin_stream()
    deadline = time.perf_counter() + 120
    while not rec.finished:
        rec.poll()
        time.sleep(0.02)
        assert time.perf_counter() < deadline, f"stuck in {rec.phase}"
    session.shutdown()
    return rec, settings, drafts, capture_s


def _record(name, encoder, echo_tags, n_frames=48):
    rec, settings, drafts, capture_s = _run_recording(name, encoder, echo_tags, n_frames)
    assert rec.phase == "DONE", rec.message
    expected = "in-process Blender" if encoder == "INPROCESS" else "background Blender"
    assert rec.report["encoder"] == expected, rec.report

    report = rec.report
    print(f"  {name}: capture {capture_s:.1f}s, report {report}")
    assert report["timeline_coverage"] > 0.9, report
    # The mock adds 1.0 s; on slow machines its CPU-bound repaint adds queueing delay on top.
    assert 0.8 < report["latency_seconds"] < 4.0, report
    if echo_tags:
        assert report["tagged_frames"] > 0

    # The mock's main frame is a deterministic function of the draft, so the aligned
    # output can be checked frame by frame against the draft it should come from.
    mock = backends.MockBackend(None)
    mock.set_reference_pixels(settings["reference_pixels"])
    seq_dir = os.path.join(rec.work_dir, "seq", "main")
    good = 0
    for i in range(n_frames):
        got = imaging.decode_png_rgb(open(os.path.join(seq_dir, f"f_{i + 1:06d}.png"), "rb").read())
        want = imaging.bgra_to_rgb(mock._stylize(drafts[i]))
        if np.abs(got.astype(int) - want.astype(int)).mean() < 2.0:
            good += 1
    print(f"  {name}: {good}/{n_frames} aligned frames exactly match their draft")
    assert good >= 0.9 * n_frames

    for key, width, frames in (("main", 1280, n_frames), ("side_by_side", 2560, n_frames), ("raw", 1280, None)):
        path = rec.outputs[key]
        assert os.path.getsize(path) > 10000, path
        info = ffprobe(path)
        if info is not None:
            assert (info["width"], info["height"]) == (width, 704), info
            if frames is not None:
                assert int(info["nb_read_frames"]) == frames, info
    return rec


@check("record pipeline: mock, draft matching, background Blender encoder")
def test_record_background():
    _record("take_background", "AUTO", echo_tags=False)


@check("record pipeline: mock, frame tags, in-process Blender encoder")
def test_record_inprocess():
    _record("take_inprocess", "INPROCESS", echo_tags=True)


@check("api url: hardcoded default, normalization, REACTOR_API_URL override")
def test_api_url():
    norm = backends.normalize_api_url
    assert norm("") == norm(None) == norm("   ") == "https://api.reactor.inc"
    assert norm("api.reactor.inc") == "https://api.reactor.inc"
    assert norm("api.reactor.inc/") == "https://api.reactor.inc"
    assert norm(" https://api.reactor.inc/// ") == "https://api.reactor.inc"
    assert norm("HTTPS://api.reactor.inc") == "https://api.reactor.inc"
    assert norm("http://localhost:8080/") == "http://localhost:8080"
    assert norm("staging.example.com/api/") == "https://staging.example.com/api"
    for bad in ("ftp://api.reactor.inc", "https://", "://"):
        try:
            norm(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should be rejected")
    old = os.environ.pop("REACTOR_API_URL", None)
    try:
        assert backends.api_url() == "https://api.reactor.inc"
        os.environ["REACTOR_API_URL"] = "staging.example.com/"
        assert backends.api_url() == "https://staging.example.com"
    finally:
        os.environ.pop("REACTOR_API_URL", None)
        if old is not None:
            os.environ["REACTOR_API_URL"] = old
    backend = backends.ReactorBackend(None, "rk_test", api_url="api.reactor.inc/")
    assert backend._api_url == "https://api.reactor.inc"
    assert backends.ReactorBackend(None, "rk_test")._api_url == "https://api.reactor.inc"


@check("user-visible settings: preferences hold only the API key")
def test_settings_surface():
    from eden_live_previs import prefs as prefs_module, props

    assert set(prefs_module.EDENLIVE_AP_preferences.__annotations__) == {"api_key"}
    assert set(props.EdenLiveSettings.__annotations__) == {
        "reference_path", "prompt", "prompt_text", "seed", "source_mode", "fps", "hide_overlays", "show_draft",
        "record_dir", "record_name", "record_method", "record_tail", "record_keep_frames",
    }
    old = os.environ.pop("EDEN_LIVE_MOCK", None)
    try:
        assert prefs_module.use_mock(), "fresh startup must be offline"
        bpy.context.window_manager.eden_live_runtime.backend_mode = "PAID"
        assert not prefs_module.use_mock()
        os.environ["EDEN_LIVE_MOCK"] = "1"
        assert prefs_module.use_mock()
    finally:
        os.environ.pop("EDEN_LIVE_MOCK", None)
        if old is not None:
            os.environ["EDEN_LIVE_MOCK"] = old
        bpy.context.window_manager.eden_live_runtime.backend_mode = "MOCK"


@check("record: background-encode failure falls back to encoding in-process")
def test_background_fallback():
    real = encode.start_background_encode

    def failing(jobs, work_dir, blender_binary=None):
        log = os.path.join(work_dir, "encode.log")
        with open(log, "w") as f:
            f.write("simulated background crash\n")
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"]), log

    encode.start_background_encode = failing
    try:
        rec, *_ = _run_recording("take_fallback", "AUTO", echo_tags=True, n_frames=12, keep_frames=False)
    finally:
        encode.start_background_encode = real
    assert rec.phase == "DONE", rec.message
    assert rec.report["encoder"] == "in-process Blender", rec.report
    assert os.path.getsize(rec.outputs["main"]) > 10000
    assert not os.path.isdir(rec.work_dir), "work dir should be cleaned up after success"


@check("record: a failed encode keeps the PNG frames and says where")
def test_failed_encode_keeps_frames():
    real_start, real_encode = encode.start_background_encode, encode.blender_encode
    encode.start_background_encode = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated: cannot start"))
    encode.blender_encode = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated encoder failure"))
    try:
        rec, *_ = _run_recording("take_failed", "AUTO", echo_tags=True, n_frames=12, keep_frames=False)
    finally:
        encode.start_background_encode, encode.blender_encode = real_start, real_encode
    assert rec.phase == "ERROR", rec.phase
    assert "simulated encoder failure" in rec.message and "cannot start" in rec.message, rec.message
    assert rec.kept_frames and "PNG frames were kept" in rec.message, rec.message
    main_dir = os.path.join(rec.kept_frames, "main")
    pngs = [n for n in os.listdir(main_dir) if n.endswith(".png")]
    assert len(pngs) == 12, pngs
    assert not os.path.exists(os.path.join(rec.work_dir, "drafts.npy"))
    print(f"  kept {len(pngs)} aligned frames in {main_dir}")


def main():
    for test in (test_register, test_imaging, test_align, test_api_url, test_settings_surface, test_demo_capture,
                 test_mock_session, test_record_background, test_record_inprocess, test_background_fallback,
                 test_failed_encode_keeps_frames):
        test()
    try:
        addon.unregister()
        RESULTS.append(("unregister add-on", True, 0, ""))
        print("PASS unregister add-on")
    except Exception:
        RESULTS.append(("unregister add-on", False, 0, traceback.format_exc()))
        print(f"FAIL unregister\n{traceback.format_exc()}")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed; outputs in {OUT}")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


main()
