"""Map frames returned by EdenLive back onto timeline frames.

EdenLive returns ``draft_video``: the source frame each generated frame was made
from. While recording we keep a thumbnail of every frame we sent, so each returned
draft can be matched to the timeline frame it came from, which tells us where the
matching ``main_video`` frame belongs. Frame tags (``user_data``) are used instead
when the model echoes them.

Returned frames come back in order, roughly one timeline step per frame (repeats
while we hold the first/last frame, occasional drops), so the assignment is solved
as a monotonic path (Viterbi) over: image distance to each sent thumbnail, measured
against the stream's noise floor, plus a timing prior from the measured latency.
"""

import numpy as np

STAY_COST = 0.05
SKIP_COST = 0.5
MAX_STEP = 4
TAG_MISMATCH = 1e6


def _pairwise_mse(a, b):
    a = a.reshape(len(a), -1).astype(np.float64)
    b = b.reshape(len(b), -1).astype(np.float64)
    d = (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2.0 * a @ b.T
    return np.maximum(d, 0.0) / a.shape[1]


def _estimate_latency(dist, rows, arrivals, sent_times, tags, default):
    samples = []
    tagged = tags >= 0
    samples += list(arrivals[tagged] - sent_times[tags[tagged]])
    if len(rows):
        n = dist.shape[1]
        best = dist.argmin(axis=1)
        masked = dist.copy()
        masked[np.abs(np.arange(n)[None, :] - best[:, None]) <= 3] = np.inf
        second = masked.min(axis=1)
        best_d = dist[np.arange(len(rows)), best]
        confident = np.isfinite(second) & (second > 4.0 * best_d + 1e-5)
        samples += list(arrivals[rows[confident]] - sent_times[best[confident]])
    samples = [s for s in samples if np.isfinite(s)]
    return float(np.median(samples)) if samples else default


def _viterbi(emission):
    r, n = emission.shape
    steps = np.arange(MAX_STEP)
    trans = np.where(steps == 0, STAY_COST, np.where(steps == 1, 0.0, SKIP_COST * (steps - 1)))
    cost = emission[0].copy()
    back = np.zeros((r, n), np.int64)
    for i in range(1, r):
        candidates = np.full((MAX_STEP, n), np.inf)
        for k in range(MAX_STEP):
            candidates[k, k:] = cost[: n - k] + trans[k]
        choice = candidates.argmin(axis=0)
        back[i] = np.arange(n) - choice
        cost = candidates[choice, np.arange(n)] + emission[i]
    path = np.empty(r, np.int64)
    path[-1] = int(cost.argmin())
    for i in range(r - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path


def align(sent_thumbs, sent_times, received, fps, default_latency=1.0):
    """Assign each received frame to a timeline index.

    sent_thumbs: (N, th, tw) thumbnails of timeline frames 0..N-1.
    sent_times:  (N,) perf_counter time each frame was pushed during playback.
    received:    list of dicts with ``thumb`` (draft thumbnail or None),
                 ``arrival`` (perf_counter) and optional ``tag_index``.
    Returns (indices ndarray, info dict).
    """
    n = len(sent_thumbs)
    r = len(received)
    info = {"tagged": 0, "matched": 0, "latency": default_latency, "frames": r}
    if r == 0 or n == 0:
        return np.zeros(r, np.int64), info

    sent_times = np.asarray(sent_times, np.float64)
    if np.isnan(sent_times).any():
        known = np.where(~np.isnan(sent_times))[0]
        sent_times = np.interp(np.arange(n), known, sent_times[known]) if len(known) else np.arange(n) / fps
    arrivals = np.array([item["arrival"] for item in received], np.float64)
    tags = np.array(
        [t if (t := item.get("tag_index")) is not None and 0 <= t < n else -1 for item in received], np.int64
    )
    rows = np.array([i for i, item in enumerate(received) if item.get("thumb") is not None and tags[i] < 0], np.int64)
    info["tagged"] = int((tags >= 0).sum())
    info["matched"] = int(len(rows))

    dist = None
    if len(rows):
        dist = _pairwise_mse(np.stack([received[i]["thumb"] for i in rows]), np.asarray(sent_thumbs))
    latency = _estimate_latency(dist if dist is not None else np.zeros((0, n)), rows, arrivals, sent_times, tags,
                                default_latency)
    info["latency"] = latency

    cols = np.arange(n)[None, :]
    expected = np.clip(np.searchsorted(sent_times, arrivals - latency, side="right") - 1, 0, n - 1)
    sigma = max(1.0, 0.15 * fps)
    emission = 0.5 * ((cols - expected[:, None]) / sigma) ** 2
    if dist is not None:
        noise = max(float(np.median(dist.min(axis=1))), 1e-6)
        emission[rows] += dist / noise
    tagged = np.where(tags >= 0)[0]
    if len(tagged):
        emission[tagged] = np.where(cols == tags[tagged][:, None], 0.0, TAG_MISMATCH)
    return _viterbi(emission), info


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
