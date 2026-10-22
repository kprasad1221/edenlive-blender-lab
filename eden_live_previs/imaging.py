"""Pixel helpers shared by capture, preview and recording.

Pure numpy (plus zlib), no bpy, so it can be unit-tested outside Blender.

Conventions:
- "BGRA" arrays are uint8, shape (h, w, 4), top row first. This is what the
  Reactor SDK sends and receives.
- Blender's GPU read-backs and ``Image.pixels`` are bottom row first, RGBA.
"""

import struct
import zlib

import numpy as np

EDEN_W = 1280
EDEN_H = 704


def gpu_rgba_to_bgra(rgba_bottom_up):
    """GPU framebuffer read-back (RGBA, bottom-up) -> BGRA top-down."""
    out = np.ascontiguousarray(rgba_bottom_up[::-1, :, [2, 1, 0, 3]])
    out[..., 3] = 255
    return out


def bgra_to_rgb(bgra):
    return np.ascontiguousarray(bgra[..., [2, 1, 0]])


def rgb_to_bgra(rgb):
    h, w = rgb.shape[:2]
    out = np.empty((h, w, 4), np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = 255
    return out


def cover_resize(arr, width, height):
    """Nearest-neighbour scale-to-cover then center-crop, like EdenLive does."""
    src_h, src_w = arr.shape[:2]
    if (src_w, src_h) == (width, height):
        return arr
    scale = max(width / src_w, height / src_h)
    x0 = (src_w * scale - width) / 2.0
    y0 = (src_h * scale - height) / 2.0
    xs = ((np.arange(width) + x0 + 0.5) / scale).astype(np.intp).clip(0, src_w - 1)
    ys = ((np.arange(height) + y0 + 0.5) / scale).astype(np.intp).clip(0, src_h - 1)
    return arr[ys][:, xs]


def fit_height(arr, height):
    """Nearest-neighbour resize keeping aspect ratio so the result is `height` tall."""
    src_h, src_w = arr.shape[:2]
    if src_h == height:
        return arr
    width = max(1, round(src_w * height / src_h))
    return cover_resize(arr, width, height)


THUMB_W = 96
THUMB_H = 54


def thumbnail(bgra):
    """Small grayscale float32 signature used to match returned draft frames to sent ones."""
    small = cover_resize(bgra, THUMB_W * 4, THUMB_H * 4)[..., :3].astype(np.float32)
    gray = small.mean(axis=2)
    return gray.reshape(THUMB_H, 4, THUMB_W, 4).mean(axis=(1, 3)) / 255.0


def side_by_side(left, right):
    """Concatenate two frames horizontally, matching heights."""
    if left.shape[0] != right.shape[0]:
        left = fit_height(left, right.shape[0])
    return np.concatenate([left, right], axis=1)


def bgra_to_image_pixels(bgra):
    """BGRA top-down uint8 -> flat float32 RGBA bottom-up, ready for ``Image.pixels.foreach_set``."""
    h, w = bgra.shape[:2]
    out = np.empty((h, w, 4), np.float32)
    flipped = bgra[::-1]
    np.multiply(flipped[..., 2], 1.0 / 255.0, out=out[..., 0], casting="unsafe")
    np.multiply(flipped[..., 1], 1.0 / 255.0, out=out[..., 1], casting="unsafe")
    np.multiply(flipped[..., 0], 1.0 / 255.0, out=out[..., 2], casting="unsafe")
    out[..., 3] = 1.0
    return out.reshape(-1)


def image_pixels_to_bgra(flat, width, height):
    """``Image.pixels`` float RGBA bottom-up -> BGRA top-down uint8."""
    rgba = (np.asarray(flat, np.float32).reshape(height, width, 4).clip(0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return gpu_rgba_to_bgra(rgba)


def _png_chunk(tag, data):
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_png(rgb, level=1):
    """Encode an RGB uint8 array (h, w, 3) as PNG bytes."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    raw = np.empty((h, w * 3 + 1), np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rgb.reshape(h, w * 3)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(raw.tobytes(), level)),
            _png_chunk(b"IEND", b""),
        )
    )


def write_png(path, rgb, level=1):
    with open(path, "wb") as f:
        f.write(encode_png(rgb, level))


def decode_png_rgb(data):
    """Decode PNGs written by `encode_png` (8-bit RGB, filter 0 only). Used by tests and the mock."""
    pos = 8
    width = height = None
    idat = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype != 2:
                raise ValueError("only 8-bit RGB PNGs are supported")
        elif tag == b"IDAT":
            idat.append(body)
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(height, width * 3 + 1)
    if np.any(raw[:, 0] != 0):
        raise ValueError("only unfiltered PNG rows are supported")
    return raw[:, 1:].reshape(height, width, 3).copy()
