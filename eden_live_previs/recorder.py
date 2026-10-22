"""Record the timeline through EdenLive.

1. CAPTURE (main thread, one frame per tick): step through the frame range and
   render each draft frame into a memory-mapped file. Rendering can be slower
   than real time; that's fine because nothing is streamed yet.
2. STREAM (session threads): start a fresh world, hold frame 0 until the first
   generated chunk arrives, then play the pre-rendered frames at an exact fixed
   rate, hold the last frame for a tail (covering latency), and pause the world.
   Returned frames are written to PNG by a writer thread as they arrive.
3. FINALIZE: pair main/draft frames, align them to timeline frames (see align.py)
   and encode MP4s: aligned main, aligned draft|main side by side, and the raw
   stream as it arrived.
"""

import asyncio
import json
import os
import queue
import shutil
import threading
import time

import numpy as np

from . import align, encode, imaging
from .session import UserError

W, H = imaging.EDEN_W, imaging.EDEN_H


class PlaylistProvider:
    """Feeds the session's sender thread while recording."""

    def __init__(self, frames, fps, tail_seconds):
        self.frames = frames
        self.n = len(frames)
        self.state = "HOLD"
        self.index = 0
        self.sent_times = np.full(self.n, np.nan)
        self.tail_left = max(1, int(round(tail_seconds * fps)))
        self.pushes = 0

    def go(self):
        self.state = "PLAY"

    @property
    def finished(self):
        return self.state == "DONE"

    def next_frame(self, now):
        if self.state == "HOLD":
            idx = 0
        elif self.state == "PLAY":
            idx = self.index
            self.sent_times[idx] = now
            self.index += 1
            if self.index >= self.n:
                self.state = "TAIL"
        else:
            idx = self.n - 1
            if self.state == "TAIL":
                self.tail_left -= 1
                if self.tail_left <= 0:
                    self.state = "DONE"
        self.pushes += 1
        return np.array(self.frames[idx]), json.dumps({"i": int(idx)}).encode()


class FrameWriter:
    """Writes returned frames to PNG off the delivery thread."""

    def __init__(self, root):
        self.root = root
        self.dirs = {"main_video": os.path.join(root, "main"), "draft_video": os.path.join(root, "draft")}
        for d in self.dirs.values():
            os.makedirs(d, exist_ok=True)
        self.entries = {"main_video": [], "draft_video": []}
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="EdenLiveWriter", daemon=True)
        self._thread.start()
        self.error = None

    def put(self, item):
        self._queue.put(item)

    def close(self):
        self._queue.put(None)
        self._thread.join()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            track, bgra, frame_id, ts, user_data, arrival = item
            try:
                frame = imaging.cover_resize(bgra, W, H)
                entries = self.entries[track]
                prefix = "m" if track == "main_video" else "d"
                path = os.path.join(self.dirs[track], f"{prefix}_{len(entries) + 1:06d}.png")
                imaging.write_png(path, imaging.bgra_to_rgb(frame), level=1)
                tag_index = None
                if user_data:
                    try:
                        tag_index = int(json.loads(user_data)["i"])
                    except Exception:
                        tag_index = None
                entries.append(
                    {
                        "path": path,
                        "arrival": arrival,
                        "ts": ts,
                        "frame_id": frame_id,
                        "tag_index": tag_index,
                        "thumb": imaging.thumbnail(frame) if track == "draft_video" else None,
                    }
                )
            except Exception as exc:
                self.error = exc


def _pair_drafts(mains, drafts):
    """Index of the draft frame belonging to each main frame (or -1)."""
    if not drafts:
        return [-1] * len(mains)
    use_ts = all(e["ts"] for e in mains) and all(e["ts"] for e in drafts)
    key = "ts" if use_ts else "arrival"
    times = np.array([d[key] for d in drafts], np.float64)
    out = []
    for m in mains:
        pos = int(np.searchsorted(times, m[key]))
        best = min((p for p in (pos - 1, pos) if 0 <= p < len(times)), key=lambda p: abs(times[p] - m[key]))
        out.append(best)
    return out


class Recorder:
    def __init__(self, session, settings):
        """settings: dict with frames, stream_fps, output_fps, out_dir, name, tail_seconds,
        reference_path, prompt, seed, reference_pixels, encoder ('AUTO'|'FFMPEG'|'BLENDER'),
        ffmpeg_path, keep_frames, warmup_timeout."""
        self.session = session
        self.s = settings
        self.frames = list(settings["frames"])
        self.n = len(self.frames)
        if self.n == 0:
            raise UserError("The frame range is empty.")
        self.out_dir = settings["out_dir"]
        self.name = settings["name"]
        self.work_dir = os.path.join(self.out_dir, f"{self.name}_work")
        if os.path.isdir(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)
        os.makedirs(self.work_dir, exist_ok=True)
        self.drafts = np.lib.format.open_memmap(
            os.path.join(self.work_dir, "drafts.npy"), mode="w+", dtype=np.uint8, shape=(self.n, H, W, 4)
        )
        self.thumbs = np.zeros((self.n, imaging.THUMB_H, imaging.THUMB_W), np.float32)
        self.index = 0
        self.phase = "CAPTURE"
        self.message = "Rendering draft frames"
        self.error = ""
        self.outputs = {}
        self.report = {}
        self.provider = None
        self.writer = None
        self.recording = False
        self._main_since_start = 0
        self._future = None
        self._encode_thread = None
        self._encode_error = ""
        self._sequences = {}
        self._prev_fps = session.fps

    @property
    def progress(self):
        if self.phase == "CAPTURE":
            return self.index / self.n
        if self.phase in ("PLAY", "TAIL") and self.provider is not None:
            return min(1.0, self.provider.index / self.n)
        return 1.0 if self.phase == "DONE" else 0.0

    @property
    def finished(self):
        return self.phase in ("DONE", "ERROR", "CANCELLED")

    # -------------------------------------------------------------- capture

    def add_frame(self, bgra):
        frame = imaging.cover_resize(bgra, W, H)
        self.drafts[self.index] = frame
        self.thumbs[self.index] = imaging.thumbnail(frame)
        self.index += 1
        self.message = f"Rendering draft frame {self.index}/{self.n}"
        if self.index >= self.n:
            self.drafts.flush()
            return True
        return False

    # -------------------------------------------------------------- stream

    def begin_stream(self):
        s = self.s
        self.provider = PlaylistProvider(self.drafts, s["stream_fps"], s["tail_seconds"])
        self.writer = FrameWriter(self.work_dir)
        self.session.add_sink(self._sink)
        self.session.fps = s["stream_fps"]
        self.session.frame_provider = self.provider
        self.phase = "CONNECTING"
        self.message = "Waiting for EdenLive"
        self._future = self.session.wait_ready_and(self._stream)

    def _sink(self, track, bgra, frame_id, ts, user_data, arrival):
        if self.phase == "WARMUP" and track == "main_video":
            self._main_since_start += 1
        if self.recording:
            self.writer.put((track, bgra, frame_id, ts, user_data, arrival))

    async def _stream(self):
        s = self.s
        session = self.session
        self.phase = "WARMUP"
        self.message = "Starting a new world"
        await session._start_world(s["reference_path"], s["prompt"], s["seed"], s.get("reference_pixels"))
        world = session.state.get("world_id")
        self._main_since_start = 0
        self.message = "Waiting for the first generated chunk"
        deadline = time.perf_counter() + s.get("warmup_timeout", 60.0)
        while True:
            st = session.state
            if st.get("world_id") == world and (st.get("chunks") or 0) >= 1 and self._main_since_start > 0:
                break
            if time.perf_counter() > deadline:
                raise UserError("EdenLive produced no frames in time; check the log and try again.")
            if self.phase == "CANCELLED":
                return
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        self.recording = True
        self.phase = "PLAY"
        self.message = "Streaming the timeline"
        self.provider.go()
        while not self.provider.finished:
            if self.phase == "CANCELLED":
                return
            if self.provider.state == "TAIL":
                self.phase = "TAIL"
                self.message = "Collecting the last generated frames"
            await asyncio.sleep(0.02)
        self.recording = False
        await session._command("set_paused", {"paused": True})

    def _end_stream(self):
        self.recording = False
        self.session.remove_sink(self._sink)
        self.session.frame_provider = None
        self.session.fps = self._prev_fps

    # -------------------------------------------------------------- finalize

    def poll(self):
        """Advance non-capture phases. Call regularly from the main thread."""
        if self.phase in ("CONNECTING", "WARMUP", "PLAY", "TAIL") and self._future is not None and self._future.done():
            self._end_stream()
            exc = self._future.exception()
            if exc is not None:
                self._fail(str(exc))
                return
            if self.phase == "CANCELLED":
                return
            self.writer.close()
            if self.writer.error is not None:
                self._fail(f"Writing frames failed: {self.writer.error}")
                return
            self.phase = "FINALIZE"
            self.message = "Aligning frames"
            try:
                self._finalize()
            except Exception as exc:
                self._fail(f"Finalizing failed: {exc}")
        elif self.phase == "ENCODE" and self._encode_thread is not None and not self._encode_thread.is_alive():
            if self._encode_error:
                self._fail(self._encode_error)
            else:
                self._done()

    def _finalize(self):
        mains = self.writer.entries["main_video"]
        drafts = self.writer.entries["draft_video"]
        if not mains:
            raise UserError("No generated frames were received.")
        pairs = _pair_drafts(mains, drafts)
        received = []
        for m, d in zip(mains, pairs):
            draft = drafts[d] if d >= 0 else None
            tag = m["tag_index"] if m["tag_index"] is not None else (draft["tag_index"] if draft else None)
            received.append({"thumb": draft["thumb"] if draft else None, "arrival": m["arrival"], "tag_index": tag})
        indices, info = align.align(self.thumbs, self.provider.sent_times, received, self.s["stream_fps"])
        choice, coverage = align.pick_frames(indices, self.n)

        seq_root = os.path.join(self.work_dir, "seq")
        main_files = [mains[c]["path"] for c in choice]
        draft_files = [drafts[pairs[c]]["path"] if pairs[c] >= 0 else mains[c]["path"] for c in choice]
        self._sequences = {
            "main": (os.path.join(seq_root, "main"), encode.link_sequence(main_files, os.path.join(seq_root, "main"))),
            "draft": (os.path.join(seq_root, "draft"), encode.link_sequence(draft_files, os.path.join(seq_root, "draft"))),
            "raw": (
                os.path.join(seq_root, "raw"),
                encode.link_sequence([m["path"] for m in mains], os.path.join(seq_root, "raw")),
            ),
        }
        self.report = {
            "timeline_frames": self.n,
            "frame_numbers": [self.frames[0], self.frames[-1]],
            "stream_fps": self.s["stream_fps"],
            "output_fps": self.s["output_fps"],
            "received_main": len(mains),
            "received_draft": len(drafts),
            "latency_seconds": round(info["latency"], 3),
            "tagged_frames": info["tagged"],
            "matched_by_draft": info["matched"],
            "timeline_coverage": round(coverage, 3),
            "backend": self.session.backend_label,
            "prompt": self.s["prompt"],
            "seed": self.s["seed"],
        }
        base = os.path.join(self.out_dir, self.name)
        self.outputs = {
            "main": base + "_main.mp4",
            "side_by_side": base + "_side_by_side.mp4",
            "raw": base + "_raw.mp4",
            "report": base + "_report.json",
        }
        with open(self.outputs["report"], "w") as f:
            json.dump(self.report, f, indent=2)

        self.phase = "ENCODE"
        self.message = "Encoding MP4"
        ffmpeg = encode.find_ffmpeg(self.s.get("ffmpeg_path", ""))
        encoder = self.s.get("encoder", "AUTO")
        if encoder == "FFMPEG" and not ffmpeg:
            raise UserError("ffmpeg not found; set its path in the add-on preferences or use the Blender encoder.")
        if ffmpeg and encoder != "BLENDER":
            self._encode_error = ""
            self._encode_thread = threading.Thread(target=self._encode_ffmpeg, args=(ffmpeg,), daemon=True)
            self._encode_thread.start()
        else:
            self._encode_blender()
            self._done()

    def _encode_ffmpeg(self, ffmpeg):
        seq = self._sequences
        try:
            fps = self.s["output_fps"]
            encode.ffmpeg_encode(ffmpeg, self.outputs["main"], fps, [seq["main"][0]])
            encode.ffmpeg_encode(ffmpeg, self.outputs["side_by_side"], fps, [seq["draft"][0], seq["main"][0]])
            encode.ffmpeg_encode(ffmpeg, self.outputs["raw"], self.s["stream_fps"], [seq["raw"][0]])
        except Exception as exc:
            self._encode_error = str(exc)

    def _encode_blender(self):
        seq = self._sequences
        fps = self.s["output_fps"]
        main_dir, main_names = seq["main"]
        draft_dir, draft_names = seq["draft"]
        raw_dir, raw_names = seq["raw"]
        encode.blender_encode(self.outputs["main"], fps, [(main_dir, main_names, W)], H)
        encode.blender_encode(
            self.outputs["side_by_side"], fps, [(draft_dir, draft_names, W), (main_dir, main_names, W)], H
        )
        encode.blender_encode(self.outputs["raw"], self.s["stream_fps"], [(raw_dir, raw_names, W)], H)

    def _done(self):
        self.phase = "DONE"
        self.message = f"Saved {os.path.basename(self.outputs['main'])} ({self.report['timeline_coverage']:.0%} of frames matched)"
        self._cleanup()

    def _fail(self, text):
        self.phase = "ERROR"
        self.error = self.session.redact(text)
        self.message = self.error
        self._cleanup()

    def cancel(self):
        if self.finished:
            return
        streaming = self.phase in ("CONNECTING", "WARMUP", "PLAY", "TAIL")
        self.phase = "CANCELLED"
        self.message = "Recording cancelled"
        if self.provider is not None:
            self._end_stream()
            if streaming and self.session.ready:
                self.session.set_paused(True)
        if self.writer is not None:
            self.writer.close()
        self._cleanup()

    def _cleanup(self):
        if self.provider is not None:
            self.provider.frames = None
        self.drafts = None
        if self.s.get("keep_frames"):
            try:
                os.remove(os.path.join(self.work_dir, "drafts.npy"))
            except OSError:
                pass
        else:
            shutil.rmtree(self.work_dir, ignore_errors=True)
