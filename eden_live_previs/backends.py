"""Model backends: the real Reactor EdenLive session and an offline mock.

Both expose the same small async surface, used only from the session's
asyncio thread (except ``push_frame``, which the sender thread calls):

    await connect() / disconnect()
    await upload_file(path) -> file reference
    await send_command(name, data) -> reply dict | None   (raises CommandRefused)
    push_frame(bgra, user_data)

and report back through a callbacks object (the session):

    on_frame(track, bgra, frame_id, timestamp_us, user_data)   # any thread
    on_message(msg) / on_status(status) / on_error(code, message)
"""

import asyncio
import copy
import mimetypes
import os
import threading
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

import numpy as np

from . import imaging

MODEL_NAME = "reactor/eden-live"
SOURCE_TRACK = "source_video"
OUTPUT_TRACKS = ("main_video", "draft_video")
DEFAULT_API_URL = "https://api.reactor.inc"


def normalize_api_url(value):
    """Turn user input like ``api.reactor.inc/`` into ``https://api.reactor.inc``.

    The SDK builds request URLs by appending paths such as ``/tokens``, so a missing
    scheme fails with "unknown url type" and a trailing slash doubles it.
    """
    url = (value or "").strip()
    if not url:
        return DEFAULT_API_URL
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"API URL must use https:// (got {value!r}).")
    if not parts.netloc:
        raise ValueError(f"API URL has no host (got {value!r}).")
    return f"{scheme}://{parts.netloc}{parts.path}".rstrip("/")


class CommandRefused(Exception):
    def __init__(self, command, code, message):
        super().__init__(f"{command} refused ({code or 'error'}): {message}")
        self.command = command
        self.code = code
        self.message = message


class ReactorBackend:
    label = "Reactor"

    def __init__(self, callbacks, api_key, model=MODEL_NAME, api_url=None, max_session_seconds=None):
        self._cb = callbacks
        self._key = api_key
        self._model = model
        self._api_url = normalize_api_url(api_url)
        self._max_session_seconds = max_session_seconds
        self._reactor = None
        self._source = None

    async def connect(self):
        from reactor_sdk import Reactor

        kwargs = {"api_url": self._api_url}
        if self._max_session_seconds:
            kwargs["max_session_duration_seconds"] = int(self._max_session_seconds)
        reactor = Reactor(model_name=self._model, api_key=self._key, **kwargs)
        self._key = None
        reactor.on("status_changed", lambda status: self._cb.on_status(str(getattr(status, "value", status))))
        reactor.on("message", self._cb.on_message)
        reactor.on("error", self._on_error)
        for name in OUTPUT_TRACKS:
            reactor.track(name).on_raw_frame(self._frame_handler(name))
        self._reactor = reactor
        await reactor.connect()
        self._source = await reactor.track(SOURCE_TRACK).publish()
        try:
            await reactor.set_bitrate(max_bps=12_000_000)
            await self._source.set_bitrate(max_bps=8_000_000)
        except Exception:
            pass

    def _frame_handler(self, name):
        def handler(bgra, width, height, frame_id, timestamp_us, user_data):
            frame = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)
            self._cb.on_frame(name, frame, frame_id, timestamp_us, user_data)

        return handler

    def _on_error(self, err):
        self._cb.on_error(getattr(err, "code", None), getattr(err, "message", None) or str(err))

    async def disconnect(self):
        reactor, self._reactor, self._source = self._reactor, None, None
        if reactor is not None:
            await reactor.disconnect()

    async def upload_file(self, path):
        return await self._reactor.upload_file(path)

    async def send_command(self, command, data):
        from reactor_sdk import ReactorError

        try:
            return await self._reactor.send_command(command, data)
        except ReactorError as err:
            raise CommandRefused(command, getattr(err, "code", None), getattr(err, "message", None) or str(err))

    def push_frame(self, bgra, user_data=None, lateness=0.0):
        """``lateness``: seconds since the frame's scheduled send time; the frame is
        stamped with that time so the stream stays evenly spaced."""
        from reactor_sdk import time_micros

        source = self._source
        if source is not None:
            stamp = time_micros() - int(max(0.0, lateness) * 1e6)
            source.push_frame(bgra, user_data=user_data or None, capture_time_us=stamp)


class MockBackend:
    """Offline stand-in for EdenLive.

    Follows the documented command preconditions and state_update payload,
    returns frames ~``latency`` seconds after they were pushed (at the rate
    they were pushed), and "generates" by repainting the draft with the
    reference image's palette plus a posterized, inked look.
    """

    label = "Mock (offline)"

    def __init__(self, callbacks, latency=1.0, chunk_frames=12, echo_tags=False):
        self._cb = callbacks
        self.latency = latency
        self.chunk_frames = chunk_frames
        self.echo_tags = echo_tags
        self._lock = threading.Lock()
        self._queue = deque()
        self._stop = threading.Event()
        self._thread = None
        self._palette = None
        self._frame_id = 0
        self._chunk_count = 0
        self._chunk_started = 0.0
        self.state = {
            "source": "game",
            "game": "",
            "image_name": "",
            "prompt": "",
            "seed": 2026092901,
            "keys": [],
            "look": [0, 0],
            "paused": True,
            "started": False,
            "ended": False,
            "game_state": "",
            "outcome": [],
            "health": None,
            "health_max": None,
            "events": [],
            "world_id": 0,
            "chunks": 0,
            "seconds": None,
        }

    async def connect(self):
        self._cb.on_status("connecting")
        await asyncio.sleep(0.2)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="EdenLiveMock", daemon=True)
        self._thread.start()
        self._cb.on_status("ready")
        self._emit_state()

    async def disconnect(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._cb.on_status("disconnected")

    def set_reference_pixels(self, bgra):
        """Optional: the session hands over the decoded reference so the mock can borrow its palette."""
        if bgra is None:
            self._palette = None
            return
        rgb = bgra[..., :3].reshape(-1, 3).astype(np.float32)
        self._palette = (rgb.mean(axis=0), rgb.std(axis=0) + 1.0)

    async def upload_file(self, path):
        size = os.path.getsize(path)
        return {
            "upload_id": str(uuid.uuid4()),
            "name": os.path.basename(path),
            "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
            "size": size,
        }

    async def send_command(self, command, data):
        await asyncio.sleep(0.02)
        with self._lock:
            st = self.state
            if command == "set_source":
                st["source"] = data["source"]
            elif command == "set_image":
                if st["started"] and not st["paused"]:
                    raise CommandRefused(command, "pause_required", "The world is not paused.")
                ref = data["image"]
                st["image_name"] = ref["name"] if isinstance(ref, dict) else getattr(ref, "name", "reference")
                st["started"] = False
            elif command == "set_prompt":
                if not str(data.get("prompt", "")).strip():
                    raise CommandRefused(command, "prompt_required", "The prompt is blank.")
                st["prompt"] = data["prompt"]
            elif command == "start":
                if st["source"] == "client" and not st["image_name"]:
                    raise CommandRefused(command, "image_required", "Client source has no staged appearance reference.")
                if not st["prompt"].strip():
                    raise CommandRefused(command, "prompt_required", "The prompt is blank.")
                st["seed"] = int(data.get("seed", 2026092901))
                st["world_id"] += 1
                st["started"] = True
                st["paused"] = False
                st["chunks"] = 0
                st["seconds"] = None
                self._queue.clear()
                self._chunk_count = 0
                self._chunk_started = time.perf_counter()
            elif command == "set_paused":
                paused = bool(data.get("paused", True))
                if not paused and not st["started"]:
                    raise CommandRefused(command, "start_required", "Call start before resuming.")
                st["paused"] = paused
                if paused:
                    self._queue.clear()
            else:
                raise CommandRefused(command, "unknown_command", f"Mock does not implement {command}.")
            snapshot = copy.deepcopy(st)
        self._cb.on_message({"type": "state_update", "data": snapshot})
        return {"type": "state_update", "data": snapshot}

    def push_frame(self, bgra, user_data=None, lateness=0.0):
        with self._lock:
            st = self.state
            if st["started"] and not st["paused"] and st["source"] == "client":
                self._queue.append((time.perf_counter() + self.latency, bgra, user_data))

    def _emit_state(self):
        with self._lock:
            snapshot = copy.deepcopy(self.state)
        self._cb.on_message({"type": "state_update", "data": snapshot})

    def _run(self):
        while not self._stop.is_set():
            item = None
            with self._lock:
                if self._queue and self._queue[0][0] <= time.perf_counter():
                    item = self._queue.popleft()
            if item is None:
                self._stop.wait(0.003)
                continue
            _, bgra, user_data = item
            draft = imaging.cover_resize(bgra, imaging.EDEN_W, imaging.EDEN_H)
            main = self._stylize(draft)
            self._frame_id += 1
            ts = int(time.time() * 1e6)
            tag = user_data if self.echo_tags else b""
            self._cb.on_frame("draft_video", draft, self._frame_id, ts, tag)
            self._cb.on_frame("main_video", main, self._frame_id, ts, tag)
            self._chunk_count += 1
            if self._chunk_count >= self.chunk_frames:
                now = time.perf_counter()
                with self._lock:
                    self.state["chunks"] += 1
                    self.state["seconds"] = round(now - self._chunk_started, 3)
                self._chunk_count = 0
                self._chunk_started = now
                self._emit_state()

    def _stylize(self, bgra):
        f = bgra[::2, ::2, :3].astype(np.float32)
        if self._palette is not None:
            mean, std = self._palette
            f = (f - f.reshape(-1, 3).mean(axis=0)) / (f.reshape(-1, 3).std(axis=0) + 1.0) * std + mean
        f = np.round(f / 40.0) * 40.0
        lum = f.mean(axis=2)
        edges = np.zeros_like(lum)
        edges[:, 1:] += np.abs(np.diff(lum, axis=1))
        edges[1:, :] += np.abs(np.diff(lum, axis=0))
        f *= (1.0 - np.clip(edges / 60.0, 0.0, 0.85))[..., None]
        h, w = f.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        vignette = 1.0 - 0.35 * (((xx / w - 0.5) ** 2 + (yy / h - 0.5) ** 2) * 2.0)
        f *= vignette[..., None]
        small = np.empty((h, w, 4), np.uint8)
        small[..., :3] = np.clip(f, 0, 255).astype(np.uint8)
        small[..., 3] = 255
        return np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)[: bgra.shape[0], : bgra.shape[1]]
