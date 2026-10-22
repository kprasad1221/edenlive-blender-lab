"""Turn PNG frame lists into MP4 files.

Two encoders:
- ``ffmpeg`` on PATH (or a path set in preferences): runs in a worker thread.
- Blender itself: a temporary scene whose Video Sequencer holds the frames,
  rendered with Blender's built-in FFmpeg (H.264). Needs no external tools but
  runs on the main thread.
"""

import os
import shutil
import subprocess
import sys


# Apps started from the macOS Finder/Dock get a minimal PATH without Homebrew/MacPorts,
# so look in the usual install locations too.
COMMON_FFMPEG_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/snap/bin",
    os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin"),
    r"C:\ffmpeg\bin",
    os.path.expanduser(r"~\scoop\shims"),
)


def find_ffmpeg(preferred="", search_dirs=COMMON_FFMPEG_DIRS):
    """ffmpeg from the preferences path, then PATH, then common install locations."""
    if preferred and os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    found = shutil.which("ffmpeg")
    if found:
        return found
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for directory in search_dirs:
        candidate = os.path.join(directory, exe)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def link_sequence(files, out_dir, prefix="f_"):
    """Materialize an ordered (possibly repeating) file list as a numbered sequence."""
    os.makedirs(out_dir, exist_ok=True)
    names = []
    for i, src in enumerate(files):
        name = f"{prefix}{i + 1:06d}.png"
        dst = os.path.join(out_dir, name)
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        names.append(name)
    return names


def _fps_arg(fps):
    return f"{fps:.6f}".rstrip("0").rstrip(".")


def ffmpeg_encode(ffmpeg, out_path, fps, sequence_dirs, prefix="f_"):
    """Encode one or more numbered sequences (stacked horizontally) to MP4."""
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for seq_dir in sequence_dirs:
        cmd += ["-framerate", _fps_arg(fps), "-i", os.path.join(seq_dir, f"{prefix}%06d.png")]
    if len(sequence_dirs) > 1:
        cmd += ["-filter_complex", f"hstack=inputs={len(sequence_dirs)}"]
    flags = {}
    if sys.platform == "win32":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    last_error = ""
    for codec in (["-c:v", "libx264", "-preset", "medium", "-crf", "18"], ["-c:v", "mpeg4", "-q:v", "3"]):
        full = cmd + codec + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        proc = subprocess.run(full, capture_output=True, text=True, **flags)
        if proc.returncode == 0:
            return out_path
        last_error = proc.stderr.strip()[-500:]
    raise RuntimeError(f"ffmpeg failed: {last_error}")


def blender_encode(out_path, fps, layers, height):
    """Encode with Blender's sequencer.

    layers: list of (sequence_dir, filenames, width); placed left to right.
    """
    import bpy

    total_w = sum(w for _, _, w in layers)
    length = min(len(names) for _, names, _ in layers)
    scene = bpy.data.scenes.new("EdenLive_Encode")
    try:
        render = scene.render
        render.resolution_x = total_w
        render.resolution_y = height
        render.resolution_percentage = 100
        render.pixel_aspect_x = render.pixel_aspect_y = 1.0
        rounded = max(1, round(fps))
        render.fps = rounded
        render.fps_base = rounded / fps
        scene.frame_start = 1
        scene.frame_end = length
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        render.use_sequencer = True
        render.use_compositing = False
        if hasattr(render.image_settings, "media_type"):  # Blender 5.0+
            render.image_settings.media_type = "VIDEO"
        render.image_settings.file_format = "FFMPEG"
        render.ffmpeg.format = "MPEG4"
        render.ffmpeg.codec = "H264"
        render.ffmpeg.constant_rate_factor = "HIGH"
        render.ffmpeg.ffmpeg_preset = "GOOD"
        render.ffmpeg.audio_codec = "NONE"
        render.use_file_extension = False
        render.filepath = out_path

        editor = scene.sequence_editor_create()
        strips = editor.strips if hasattr(editor, "strips") else editor.sequences  # renamed in 4.4
        x = -total_w / 2.0
        for channel, (seq_dir, names, width) in enumerate(layers, start=1):
            strip = strips.new_image(
                name=f"layer{channel}",
                filepath=os.path.join(seq_dir, names[0]),
                channel=channel,
                frame_start=1,
                fit_method="ORIGINAL",
            )
            for name in names[1:length]:
                strip.elements.append(name)
            strip.frame_final_duration = length
            strip.transform.offset_x = x + width / 2.0
            x += width
        bpy.ops.render.render(animation=True, scene=scene.name)
    finally:
        bpy.data.scenes.remove(scene)
    return out_path
