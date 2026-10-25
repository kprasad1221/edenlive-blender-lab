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
import traceback
from collections import deque

import numpy as np

from . import imaging
from .backends import CommandRefused
from .diagnostics import Diagnostics
from .safety import CONNECT_TIMEOUT, FIRST_FRAME_TIMEOUT, OUTPUT_STALL_TIMEOUT, CLOSE_TIMEOUT

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
        self.log = deque(maxlen=500)
        self.diagnostics = Diagnostics()
        self.is_paid = False
        self.limit_seconds = 0
        self.attempt_started = 0.0
        self.attempt_ended = 0.0
        self.close_result = "not opened"
        self.stop_reason = ""
        self._stop_requested = threading.Event()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._tasks = {}
        self._close_lock = None
        self._disconnect_future = None
        self._world_started_at = 0.0
        self._last_output_at = 0.0
        self.received_main = 0
        self.received_draft = 0
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

    def submit(self, coro, closing=False):
        """Schedule a coroutine on the session loop; errors are reported to the UI, not raised."""
        self._ensure_threads()
        self.pending += 1
        async def run():
            task = asyncio.current_task()
            self._tasks[task] = closing
            try:
                return await coro
            except Exception as exc:
                self._report_error(exc)
                if self.is_paid and not closing and self.backend is not None:
                    await self._disconnect("Operation failed")
                raise
            finally:
                self._tasks.pop(task, None)

        future = asyncio.run_coroutine_threadsafe(run(), self._loop)

        def done(f):
            self.pending -= 1
            if not f.cancelled():
                f.exception()  # Consume errors; run() recorded the traceback before cleanup.
            self._changed.set()

        future.add_done_callback(done)
        return future

    def shutdown(self, timeout=5.0):
        if self.backend is not None or self.status in ("connecting", "ready", "disconnecting"):
            try:
                self.disconnect("Blender shutdown or file change").result(timeout=max(timeout, CLOSE_TIMEOUT + 2))
            except Exception:
                pass
        self._watchdog_stop.set()
        self._stop_sender()
        self._preview_stop.set()
        self._preview_wake.set()
        if self._loop is not None:
            loop, thread = self._loop, self._loop_thread
            async def cancel_pending():
                current = asyncio.current_task()
                tasks = [t for t in asyncio.all_tasks() if t is not current]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.wait(tasks, timeout=1.0)
            try:
                asyncio.run_coroutine_threadsafe(cancel_pending(), loop).result(timeout=2.0)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
                if not loop.is_running():
                    loop.close()
        self._loop = None
        self._loop_thread = None

    # ------------------------------------------------------------------ reporting

    def add_secret(self, value):
        if value:
            self._secrets.append(value)
            self.diagnostics.protect(value)

    def redact(self, text):
        return self.diagnostics.redact(text)

    def _log(self, text):
        text = self.redact(text)
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        self.diagnostics.event("session", text)
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
        self.diagnostics.event("exception", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
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
        self.diagnostics.event("remote_status", status)
        if status == "disconnected" and self.status == "ready":
            self.disconnect("Remote connection closed")
        self._changed.set()

    def on_error(self, code, message):
        self.last_error = self.redact(f"{code or 'error'}: {message}")
        self._log("ERROR " + self.last_error)
        if self.is_paid and not self._stop_requested.is_set():
            self.disconnect("Backend error")

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
            self.received_main += 1
            self._last_output_at = time.monotonic()
            if self.received_main == 1:
                self._log("First main_video frame received")
        elif track == "draft_video":
            self.draft.put(bgra)
            self.received_draft += 1
            if self.received_draft == 1:
                self._log("First draft_video frame received")
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
        if self.status in ("connecting", "ready", "disconnecting") or self.backend is not None:
            raise UserError("Stop the current session and confirm it is closed before reconnecting.")
        self._ensure_threads()
        self._stop_requested.clear()
        self._watchdog_stop = threading.Event()
        self._disconnect_future = None
        self.attempt_started = time.monotonic()
        self.attempt_ended = 0.0
        self._world_started_at = self._last_output_at = 0.0
        self.received_main = self.received_draft = 0
        self.stop_reason = ""
        self.close_result = "opening"
        self.diagnostics.start()
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

    def disconnect(self, reason="Stop pressed"):
        if self.status == "disconnected" and self.backend is None:
            return self.submit(self._disconnect(reason), closing=True)
        self._stop_requested.set()
        self._sender_stop.set()
        self.status = "disconnecting"
        if self._disconnect_future is not None and not self._disconnect_future.done():
            return self._disconnect_future
        self._disconnect_future = self.submit(self._disconnect(reason), closing=True)
        return self._disconnect_future

    @property
    def elapsed_seconds(self):
        if not self.attempt_started:
            return 0.0
        return max(0.0, (self.attempt_ended or time.monotonic()) - self.attempt_started)

    @property
    def remaining_seconds(self):
        return max(0.0, self.limit_seconds - self.elapsed_seconds) if self.is_paid else None

    def debug_report(self):
        return {
            "report_version": 1, "attempt": self.diagnostics.attempt_id,
            "backend": self.backend_label, "paid": self.is_paid, "status": self.status,
            "limit_seconds": self.limit_seconds, "elapsed_seconds": round(self.elapsed_seconds, 2),
            "close_result": self.close_result, "stop_reason": self.stop_reason,
            "received_main": self.received_main, "received_draft": self.received_draft,
            "last_error": self.last_error, "events": self.diagnostics.snapshot(),
        }

    def _watchdog(self, stop_event):
        # A separate thread keeps counting if the Blender UI is rendering or minimized.
        while not stop_event.wait(0.1):
            reason = ""
            if self.elapsed_seconds >= self.limit_seconds:
                reason = "Paid attempt time limit reached"
            elif self._world_started_at and not self.state.get("paused", False):
                now = time.monotonic()
                if self._last_output_at < self._world_started_at:
                    if now - self._world_started_at >= FIRST_FRAME_TIMEOUT:
                        reason = "No first output frame before timeout"
                elif now - self._last_output_at >= OUTPUT_STALL_TIMEOUT:
                    reason = "Output stream stalled"
            if reason:
                self._log(reason)
                self.disconnect(reason)
                return

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
        self._close_lock = asyncio.Lock()
        try:
            if self._stop_requested.is_set():
                return
            backend = backend_factory(self)
            self.backend = backend
            self.is_paid = bool(getattr(backend, "is_paid", False))
            self.limit_seconds = getattr(backend, "max_session_seconds", 0) if self.is_paid else 0
            if self.is_paid:
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog, args=(self._watchdog_stop,), name="EdenLiveBudget", daemon=True)
                self._watchdog_thread.start()
                self._log(f"Paid session requested; server duration limit {self.limit_seconds}s; local attempt timer running")
            await asyncio.wait_for(backend.connect(), timeout=CONNECT_TIMEOUT)
            await self._command_unlocked("set_source", {"source": "client"})
        except asyncio.CancelledError:
            raise  # The Stop path owns teardown and prevents late readiness.
        except Exception:
            await self._disconnect("Connection failed")
            raise
        if self._stop_requested.is_set():
            await self._disconnect("Stopped while connecting")
            return
        self.status = "ready"
        self._start_sender()
        self._log(f"Connected to {self.backend_label}; streaming source_video at {self.fps:g} fps.")

    async def _disconnect(self, reason="Session finished"):
        if self._close_lock is None:
            self._close_lock = asyncio.Lock()
        async with self._close_lock:
            if self.backend is None and self.status == "disconnected":
                return True
            self._stop_requested.set()
            self._watchdog_stop.set()
            self.stop_reason = reason
            self.status = "disconnecting"
            self._stop_sender()
            current = asyncio.current_task()
            pending = [t for t, closing in tuple(self._tasks.items()) if t is not current and not closing]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1.0)
            self._log(f"Closing session: {reason}")
            backend = self.backend
            try:
                if backend is not None:
                    await asyncio.wait_for(backend.disconnect(), timeout=CLOSE_TIMEOUT)
            except Exception as exc:
                self.close_result = "unconfirmed"
                self.status = "error"
                self.last_error = self.redact(f"Close not confirmed ({type(exc).__name__}). Retry Stop and check the Reactor dashboard.")
                self.diagnostics.event("close_error", str(exc))
                self._log(self.last_error)
                return False
            self.backend = None
            self.attempt_ended = time.monotonic()
            self.close_result = "SDK disconnect completed" if self.is_paid else "offline backend stopped"
            self.status = "disconnected"
            self.state = {}
            self._staged_ref_key = None
            self._log(f"Disconnected; {self.close_result}.")
            return True

    async def _wait_ready_then(self, coro_factory, timeout):
        deadline = time.perf_counter() + timeout
        while self.status == "connecting" and time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
        if self.status != "ready":
            raise UserError("Not connected to EdenLive.")
        return await coro_factory()

    async def _command_unlocked(self, name, data):
        if self._stop_requested.is_set():
            raise UserError("Session is stopping.")
        backend = self.backend
        if backend is None:
            raise UserError("Not connected.")
        reply = await backend.send_command(name, data)
        if isinstance(reply, dict):
            self.on_message(reply)
        shown = {k: v for k, v in data.items() if k in ("seed", "paused", "source")}
        self._log(f"{name} {json.dumps(shown) if shown else ''} ok")
        return reply

    async def _command(self, name, data):
        async with self._cmd_lock:
            return await self._command_unlocked(name, data)

    async def _set_prompt(self, prompt):
        self.add_secret(prompt)
        async with self._cmd_lock:
            await self._command_unlocked("set_prompt", {"prompt": prompt})
            self._sent_prompt = prompt

    async def _start_world(self, reference_path, prompt, seed, reference_pixels):
        self.add_secret(prompt)
        self.add_secret(reference_path)
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
                self._log("Uploading reference" if self.is_paid else "Loading reference locally")
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
            self._world_started_at = time.monotonic()
            self._log("World started; waiting for output")

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
                    if self.is_paid:
                        self.disconnect("Frame sender failed")
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
