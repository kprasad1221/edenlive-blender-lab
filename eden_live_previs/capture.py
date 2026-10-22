"""Capture draft frames from Blender. Main thread only.

- ``OffscreenCapture``: draws the scene from the active camera (or the current
  viewport view) into a GPU offscreen buffer using a 3D Viewport's shading
  settings. Fast (a few ms), needs a visible 3D Viewport.
- ``render_capture``: renders with the scene's render engine (Workbench/EEVEE
  recommended). Slower, but works in background mode and uses final-render looks.
"""

import os
import tempfile

import bpy
import numpy as np

from . import imaging


class CaptureError(Exception):
    pass


def find_view3d():
    """Return (window, area, region, space) of the largest visible 3D Viewport, or None."""
    best = None
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None or region.width < 8 or region.height < 8:
                continue
            if best is None or region.width * region.height > best[2].width * best[2].height:
                best = (window, area, region, area.spaces.active)
    return best


class OffscreenCapture:
    def __init__(self):
        self._offscreen = None
        self._size = (0, 0)
        self._readback = None

    def free(self):
        if self._offscreen is not None:
            self._offscreen.free()
        self._offscreen = None
        self._size = (0, 0)
        self._readback = None

    def _ensure(self, width, height):
        import gpu

        if self._size != (width, height) or self._offscreen is None:
            self.free()
            self._offscreen = gpu.types.GPUOffScreen(width, height)
            self._size = (width, height)
        return self._offscreen

    def capture(self, context, mode="CAMERA", hide_overlays=True, width=imaging.EDEN_W, height=imaging.EDEN_H):
        """Return a BGRA uint8 frame (top row first)."""
        if bpy.app.background:
            raise CaptureError("Viewport capture needs Blender's UI; use the Render capture method in background mode.")
        found = find_view3d()
        if found is None:
            raise CaptureError("Open a 3D Viewport: it provides the shading used for the draft.")
        _window, _area, region, space = found
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()
        if mode == "CAMERA":
            camera = scene.camera
            if camera is None:
                raise CaptureError("The scene has no active camera.")
            view_matrix = camera.matrix_world.inverted()
            projection = camera.calc_matrix_camera(
                depsgraph,
                x=width,
                y=height,
                scale_x=scene.render.pixel_aspect_x,
                scale_y=scene.render.pixel_aspect_y,
            )
        else:
            r3d = space.region_3d
            view_matrix = r3d.view_matrix.copy()
            projection = r3d.window_matrix.copy()
            width = max(2, int(round(height * region.width / region.height / 2.0)) * 2)

        offscreen = self._ensure(width, height)
        overlay = space.overlay
        had_overlays = overlay.show_overlays
        if hide_overlays:
            overlay.show_overlays = False
        try:
            offscreen.draw_view3d(
                scene,
                context.view_layer,
                space,
                region,
                view_matrix,
                projection,
                do_color_management=True,
            )
        finally:
            if hide_overlays:
                overlay.show_overlays = had_overlays
        import gpu

        # A flat Buffer is required: numpy misreads the multi-dimensional ones, and
        # converting texture_color.read() element by element costs ~200 ms per frame.
        if self._readback is None or len(self._readback) != width * height * 4:
            self._readback = gpu.types.Buffer("UBYTE", width * height * 4)
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            framebuffer.read_color(0, 0, width, height, 4, 0, "UBYTE", data=self._readback)
        rgba = np.frombuffer(self._readback, np.uint8).reshape(height, width, 4)
        return imaging.gpu_rgba_to_bgra(rgba)


def render_capture(scene, width=imaging.EDEN_W, height=imaging.EDEN_H, tmp_dir=None):
    """Render the current frame with the scene's engine; returns BGRA uint8."""
    if scene.camera is None:
        raise CaptureError("The scene has no active camera.")
    render = scene.render
    settings = render.image_settings
    saved = (
        render.resolution_x,
        render.resolution_y,
        render.resolution_percentage,
        render.filepath,
        render.use_file_extension,
        settings.file_format,
        settings.color_mode,
        settings.color_depth,
    )
    path = os.path.join(tmp_dir or tempfile.gettempdir(), "_eden_live_capture.png")
    try:
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.filepath = path
        render.use_file_extension = False
        settings.file_format = "PNG"
        settings.color_mode = "RGBA"
        settings.color_depth = "8"
        bpy.ops.render.render(write_still=True, scene=scene.name)
    finally:
        (
            render.resolution_x,
            render.resolution_y,
            render.resolution_percentage,
            render.filepath,
            render.use_file_extension,
            settings.file_format,
            settings.color_mode,
            settings.color_depth,
        ) = saved
    return load_image_bgra(path)


def load_image_bgra(path, max_side=None):
    """Load any image Blender can read as BGRA uint8 (top row first)."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        if width == 0 or height == 0:
            raise CaptureError(f"Could not read image: {path}")
        if max_side and max(width, height) > max_side:
            scale = max_side / max(width, height)
            image.scale(max(1, int(width * scale)), max(1, int(height * scale)))
            width, height = image.size
        pixels = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    return imaging.image_pixels_to_bgra(pixels, width, height)


def save_bgra_png(bgra, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    imaging.write_png(path, imaging.bgra_to_rgb(bgra), level=6)
    return path
