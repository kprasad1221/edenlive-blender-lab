"""Map frames returned by EdenLive back onto timeline frames.

EdenLive returns ``draft_video``: the source frame each generated frame was made
from. While recording we keep a thumbnail of every frame we sent, so each returned
draft can be matched to the timeline frame it came from, which tells us where the
matching ``main_video`` frame belongs. Frame tags (``user_data``) are used instead
when the model echoes them.

Matching uses image distance plus a timing prior (arrival time minus the measured
latency), so static stretches of a shot, where all thumbnails look alike, still
land in the right place.
"""

import numpy as np


def _pairwise_mse(a, b):
    a = a.reshape(len(a), -1).astype(np.float64)
    b = b.reshape(len(b), -1).astype(np.float64)
    d = (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2.0 * a @ b.T
    return np.maximum(d, 0.0) / a.shape[1]


def align(sent_thumbs, sent_times, received, fps, default_latency=1.0):
    """Assign each received frame to a timeline index.

    sent_thumbs: (N, th, tw) thumbnails of timeline frames 0..N-1.
    sent_times:  (N,) perf_counter time each frame was first pushed during playback.
    received:    list of dicts with keys ``thumb`` (draft thumbnail or None),
                 ``arrival`` (perf_counter) and optional ``tag_index``.
    Returns (indices ndarray, info dict).
    """
    n = len(sent_thumbs)
    r = len(received)
    info = {"tagged": 0, "matched": 0, "latency": default_latency, "frames": r}
    if r == 0 or n == 0:
        return np.zeros(r, np.int64), info

    sent_times = np.asarray(sent_times, np.float64)
    arrivals = np.array([item["arrival"] for item in received], np.float64)
    indices = np.full(r, -1, np.int64)

    for i, item in enumerate(received):
        tag = item.get("tag_index")
        if tag is not None and 0 <= tag < n:
            indices[i] = tag
            info["tagged"] += 1

    has_thumb = np.array([item.get("thumb") is not None for item in received])
    todo = np.where((indices < 0) & has_thumb)[0]
    if len(todo):
        thumbs = np.stack([received[i]["thumb"] for i in todo])
        dist = _pairwise_mse(thumbs, np.asarray(sent_thumbs))
        best = dist.argmin(axis=1)

        # A match is "confident" when nothing far away along the timeline looks as similar.
        masked = dist.copy()
        cols = np.arange(n)[None, :]
        masked[np.abs(cols - best[:, None]) <= 3] = np.inf
        second = masked.min(axis=1)
        best_d = dist[np.arange(len(todo)), best]
        confident = np.isfinite(second) & (second > 4.0 * best_d + 1e-5)

        latency = default_latency
        tagged = indices >= 0
        samples = list(arrivals[tagged] - sent_times[indices[tagged]])
        samples += list(arrivals[todo[confident]] - sent_times[best[confident]])
        if samples:
            latency = float(np.median(samples))
        info["latency"] = latency

        expected = np.searchsorted(sent_times, arrivals[todo] - latency, side="right") - 1
        expected = np.clip(expected, 0, n - 1)
        # Distances are measured against the noise floor (typical distance of the best
        # match: compression noise on a real stream, ~0 for exact echoes), so the timing
        # prior only decides between frames that look alike within that noise.
        noise = max(float(np.median(best_d)), 1e-6)
        sigma = max(1.0, 0.25 * fps)
        cost = dist / noise + ((cols - expected[:, None]) / sigma) ** 2 * 0.5
        indices[todo] = cost.argmin(axis=1)
        info["matched"] = int(len(todo))

    missing = indices < 0
    if missing.any():
        latency = info["latency"]
        expected = np.searchsorted(sent_times, arrivals[missing] - latency, side="right") - 1
        indices[missing] = np.clip(expected, 0, n - 1)

    # Playback only moves forward; a lone backwards match is noise.
    indices = np.maximum.accumulate(indices)
    return indices, info


def pick_frames(indices, n):
    """For each timeline frame choose the last received frame mapped to it.

    Returns (choice list of received-frame positions, coverage fraction). Timeline
    frames that got no returned frame reuse the nearest earlier choice (or the
    first one at the start).
    """
    choice = [-1] * n
    for pos, idx in enumerate(indices):
        if 0 <= idx < n:
            choice[idx] = pos
    covered = sum(1 for c in choice if c >= 0)
    first = next((c for c in choice if c >= 0), -1)
    last = first
    for i in range(n):
        if choice[i] < 0:
            choice[i] = last
        else:
            last = choice[i]
    return choice, (covered / n if n else 0.0)
