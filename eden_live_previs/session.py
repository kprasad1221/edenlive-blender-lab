"""Threaded EdenLive session. No bpy imports: everything here runs off Blender's main thread.

Threads:
- ``EdenLiveLoop``: an asyncio loop that owns the backend (connect, commands, uploads).
- ``EdenLiveSender``: pushes ``source_video`` frames at a fixed rate. In live mode it
  re-sends the newest captured frame, so the model sees a steady stream even when
  Blender's main thread stalls; while recording it plays a pre-rendered frame list.
- ``EdenLivePreview``: converts the newest received frames into float pixels so the
  main thread only has to copy them into an Image.
- Frames arrive on the SDK's own delivery thread (``on_frame``).

The main thread only calls the public methods below and polls plain attributes.
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque

import numpy as np

from . import imaging
from .backends import CommandRefused

log = logging.getLogger(__name__)

FRIENDLY_CODES = {
    "pause_required": "Pause the world first (set_image and select_game need a paused world).",
    "image_required": "Stage a reference image before starting.",
    "prompt_required": "The prompt is empty.",
    "start_required": "Start a world before resuming.",
    "invalid_image": "The reference image was rejected (PNG/JPEG/WebP, at least 32x32, up to 16 MP / 25 MiB).",
}


# A push delayed by a main-thread stall (Python code holding the GIL) is sent late
# but stamped with its scheduled time; a longer stall restarts the schedule.
MAX_BACKLOG = 0.5


class UserError(Exception):
    pass


class FrameSlot:
    def __init__(self):
        self._lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.time = 0.0

    def put(self, frame):
        with self._lock:
            self.frame = frame
            self.seq += 1
            self.time = time.perf_counter()

    def get(self):
        with self._lock:
            return self.frame, self.seq

    def clear(self):
        with self._lock:
            self.frame = None
            self.seq += 1


class RateMeter:
    def __init__(self, window=2.0):
        self._window = window
        self._times = deque()
        self._lock = threading.Lock()

    def tick(self):
        now = time.perf_counter()
        with self._lock:
            self._times.append(now)
            while self._times and now - self._times[0] > self._window:
                self._times.popleft()

    def rate(self):
        now = time.perf_counter()
        with self._lock:
            while self._times and now - self._times[0] > self._window:
                self._times.popleft()
            return len(self._times) / self._window


class EdenSession:
    def __init__(self):
        self.status = "disconnected"
        self.remote_status = ""
        self.backend = None
        self.backend_label = ""
        self.state = {}
        self.last_error = ""
        self.log = deque(maxlen=40)
        self.pending = 0
        self.fps = 20.0
        self.side_by_side = False

        self.source = FrameSlot()
        self.main = FrameSlot()
        self.draft = FrameSlot()
        self.in_meter = RateMeter()
        self.out_meter = RateMeter()

        self.frame_provider = None
        self._sinks = []
        self._sinks_lock = threading.Lock()
        self._secrets = []
        self._staged_ref_key = None
        self._sent_prompt = None
        self._changed = threading.Event()

        self._loop = None
        self._loop_thread = None
        self._cmd_lock = None
        self._sender_thread = None
        self._sender_stop = threading.Event()
        self._preview_thread = None
        self._preview_stop = threading.Event()
        self._preview_wake = threading.Event()
        self._preview_lock = threading.Lock()
        self._preview = None
        self._preview_serial = 0

    # ------------------------------------------------------------------ threads

    def _ensure_threads(self):
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, name="EdenLiveLoop", daemon=True)
            self._loop_thread.start()
        if self._preview_thread is None or not self._preview_thread.is_alive():
            self._preview_stop.clear()
            self._preview_thread = threading.Thread(target=self._preview_loop, name="EdenLivePreview", daemon=True)
            self._preview_thread.start()

    def submit(self, coro):
        """Schedule a coroutine on the session loop; errors are reported to the UI, not raised."""
        self._ensure_threads()
        self.pending += 1
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def done(f):
            self.pending -= 1
            exc = f.exception() if not f.cancelled() else None
            if exc is not None:
                self._report_error(exc)
            self._changed.set()

        future.add_done_callback(done)
        return future

    def shutdown(self, timeout=5.0):
        if self.status in ("connecting", "ready"):
            try:
                self.submit(self._disconnect()).result(timeout=timeout)
            except Exception:
                pass
        self._stop_sender()
        self._preview_stop.set()
        self._preview_wake.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._loop_thread = None

    # ------------------------------------------------------------------ reporting

    def add_secret(self, value):
        if value:
            self._secrets.append(value)

    def redact(self, text):
        text = str(text)
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "rk_***")
        return text

    def _log(self, text):
        text = self.redact(text)
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        log.info("%s", text)
        self._changed.set()

    def _report_error(self, exc):
        if isinstance(exc, CommandRefused):
            text = FRIENDLY_CODES.get(exc.code) or str(exc)
            if exc.code in FRIENDLY_CODES:
                text = f"{exc.command}: {text}"
        else:
            text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, UserError) else str(exc)
        self.last_error = self.redact(text)
        self._log("ERROR " + self.last_error)

    def consume_changes(self):
        """True once after anything UI-visible changed (status, state, log)."""
        if self._changed.is_set():
            self._changed.clear()
            return True
        return False

    # ------------------------------------------------------------------ backend callbacks

    def on_status(self, status):
        self.remote_status = status
        if status == "disconnected" and self.status == "ready":
            self.status = "disconnected"
            self.last_error = f"Connection to {self.backend_label} closed (sessions end after 60 minutes); press Connect."
            self._stop_sender()
        self._changed.set()

    def on_error(self, code, message):
        self.last_error = self.redact(f"{code or 'error'}: {message}")
        self._log("ERROR " + self.last_error)

    def on_message(self, msg):
        data = msg
        if isinstance(msg, dict) and msg.get("type") == "state_update" and isinstance(msg.get("data"), dict):
            data = msg["data"]
        if isinstance(data, dict) and "paused" in data and "started" in data:
            self.state = data
            self._changed.set()

    def on_frame(self, track, bgra, frame_id, timestamp_us, user_data):
        now = time.perf_counter()
        if track == "main_video":
            self.main.put(bgra)
            self.out_meter.tick()
        elif track == "draft_video":
            self.draft.put(bgra)
        with self._sinks_lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink(track, bgra, frame_id, timestamp_us, user_data, now)
            except Exception:
                log.exception("frame sink failed")
        self._preview_wake.set()

    def add_sink(self, sink):
        with self._sinks_lock:
            self._sinks.append(sink)

    def remove_sink(self, sink):
        with self._sinks_lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    # ------------------------------------------------------------------ public API (main thread)

    @property
    def ready(self):
        return self.status == "ready"

    def connect(self, backend_factory, label):
        if self.status in ("connecting", "ready"):
            return None
        self._ensure_threads()
        self.status = "connecting"
        self.backend_label = label
        self.state = {}
        self.last_error = ""
        self._staged_ref_key = None
        self._sent_prompt = None
        self.main.clear()
        self.draft.clear()
        self._log(f"Connecting to {label}...")
        return self.submit(self._connect(backend_factory))

    def disconnect(self):
        if self.status == "disconnected" and self.backend is None:
            return None
        return self.submit(self._disconnect())

    def start_world(self, reference_path, prompt, seed, reference_pixels=None):
        return self.submit(self._start_world(reference_path, prompt, seed, reference_pixels))

    def set_paused(self, paused):
        return self.submit(self._command("set_paused", {"paused": bool(paused)}))

    def set_prompt(self, prompt):
        if not prompt.strip() or prompt == self._sent_prompt:
            return None
        return self.submit(self._set_prompt(prompt))

    def wait_ready_and(self, coro_factory, timeout=180.0):
        """Run ``coro_factory()`` on the loop once connected (used by recording)."""
        return self.submit(self._wait_ready_then(coro_factory, timeout))

    def push_source_frame(self, bgra):
        self.source.put(bgra)

    def take_preview(self, serial):
        """Return (width, height, pixels, serial) if a newer preview than ``serial`` is ready."""
        with self._preview_lock:
            if self._preview is None or self._preview[3] == serial:
                return None
            return self._preview

    # ------------------------------------------------------------------ coroutines

    async def _connect(self, backend_factory):
        self._cmd_lock = asyncio.Lock()
        try:
            backend = backend_factory(self)
            self.backend = backend
            await backend.connect()
            await self._command_unlocked("set_source", {"source": "client"})
        except BaseException:
            self.status = "error"
            await self._teardown_backend()
            raise
        self.status = "ready"
        self._start_sender()
        self._log(f"Connected to {self.backend_label}; streaming source_video at {self.fps:g} fps.")

    async def _teardown_backend(self):
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception as exc:
                log.debug("disconnect failed: %s", exc)

    async def _disconnect(self):
        self.status = "disconnecting"
        self._stop_sender()
        await self._teardown_backend()
        self.status = "disconnected"
        self.state = {}
        self._staged_ref_key = None
        self._log("Disconnected; session ended.")

    async def _wait_ready_then(self, coro_factory, timeout):
        deadline = time.perf_counter() + timeout
        while self.status == "connecting" and time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
        if self.status != "ready":
            raise UserError("Not connected to EdenLive.")
        return await coro_factory()

    async def _command_unlocked(self, name, data):
        backend = self.backend
        if backend is None:
            raise UserError("Not connected.")
        reply = await backend.send_command(name, data)
        if isinstance(reply, dict):
            self.on_message(reply)
        shown = {k: v for k, v in data.items() if k != "image"}
        self._log(f"{name} {json.dumps(shown) if shown else ''} ok")
        return reply

    async def _command(self, name, data):
        async with self._cmd_lock:
            return await self._command_unlocked(name, data)

    async def _set_prompt(self, prompt):
        async with self._cmd_lock:
            await self._command_unlocked("set_prompt", {"prompt": prompt})
            self._sent_prompt = prompt

    async def _start_world(self, reference_path, prompt, seed, reference_pixels):
        if not prompt or not prompt.strip():
            raise UserError("Write a prompt before starting.")
        if not reference_path or not os.path.isfile(reference_path):
            raise UserError("Pick a reference image (or capture one from the camera) before starting.")
        async with self._cmd_lock:
            backend = self.backend
            if backend is None:
                raise UserError("Not connected.")
            stat = os.stat(reference_path)
            ref_key = (os.path.abspath(reference_path), stat.st_mtime, stat.st_size)
            if ref_key != self._staged_ref_key or not self.state.get("image_name"):
                if self.state.get("started") and not self.state.get("paused"):
                    await self._command_unlocked("set_paused", {"paused": True})
                ref = await backend.upload_file(reference_path)
                try:
                    await self._command_unlocked("set_image", {"image": ref})
                except CommandRefused as exc:
                    if exc.code != "pause_required":
                        raise
                    await self._command_unlocked("set_paused", {"paused": True})
                    await self._command_unlocked("set_image", {"image": ref})
                self._staged_ref_key = ref_key
                if hasattr(backend, "set_reference_pixels"):
                    backend.set_reference_pixels(reference_pixels)
            if self.state.get("prompt") != prompt:
                await self._command_unlocked("set_prompt", {"prompt": prompt})
            self._sent_prompt = prompt
            await self._command_unlocked("start", {"seed": int(seed)})

    # ------------------------------------------------------------------ sender

    def _start_sender(self):
        self._stop_sender()
        self._sender_stop.clear()
        self._sender_thread = threading.Thread(target=self._sender_loop, name="EdenLiveSender", daemon=True)
        self._sender_thread.start()

    def _stop_sender(self):
        self._sender_stop.set()
        thread = self._sender_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._sender_thread = None

    def _sender_loop(self):
        next_t = time.perf_counter()
        warned = False
        while not self._sender_stop.is_set():
            period = 1.0 / max(10.0, min(60.0, float(self.fps)))
            now = time.perf_counter()
            if now < next_t:
                self._sender_stop.wait(next_t - now)
                continue
            scheduled = next_t
            next_t += period
            if now - scheduled > MAX_BACKLOG:
                scheduled = now
                next_t = now + period
            backend = self.backend
            if backend is None or self.status != "ready":
                continue
            provider = self.frame_provider
            if provider is not None:
                item = provider.next_frame(now)
            else:
                frame, _ = self.source.get()
                item = (frame, None) if frame is not None else None
            if item is None or item[0] is None:
                continue
            try:
                backend.push_frame(item[0], item[1], now - scheduled)
                self.in_meter.tick()
                warned = False
            except Exception as exc:
                if not warned:
                    self._log(f"push_frame failed: {exc}")
                    warned = True

    # ------------------------------------------------------------------ preview conversion

    def _preview_loop(self):
        last_key = None
        while not self._preview_stop.is_set():
            self._preview_wake.wait(0.25)
            self._preview_wake.clear()
            main, main_seq = self.main.get()
            draft, draft_seq = self.draft.get()
            sbs = self.side_by_side
            key = (main_seq, draft_seq if sbs else None, sbs)
            if key == last_key or (main is None and draft is None):
                continue
            last_key = key
            try:
                if sbs:
                    left = draft if draft is not None else np.zeros_like(main)
                    right = main if main is not None else np.zeros_like(draft)
                    frame = imaging.side_by_side(left, right)
                else:
                    frame = main if main is not None else draft
                pixels = imaging.bgra_to_image_pixels(frame)
            except Exception:
                log.exception("preview conversion failed")
                continue
            with self._preview_lock:
                self._preview_serial += 1
                self._preview = (frame.shape[1], frame.shape[0], pixels, self._preview_serial)


_SESSION = None


def get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = EdenSession()
    return _SESSION


def reset_session():
    global _SESSION
    if _SESSION is not None:
        _SESSION.shutdown()
    _SESSION = None
