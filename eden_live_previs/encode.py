"""Turn PNG frame sequences into MP4 with Blender's built-in FFmpeg.

The frames go into a temporary scene's Video Sequencer, which is rendered to
H.264. ``start_background_encode`` does this in a separate ``blender -b`` process
(this file is its entry script), so the UI stays responsive; ``encode_jobs`` does
it in the current process and is the fallback.

A job is a dict: ``out_path``, ``fps``, ``height`` and ``layers``, a list of
``[sequence_dir, filenames, width]`` placed left to right.
"""

import json
import os
import shutil
import subprocess
import sys


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


def blender_encode(out_path, fps, layers, height):
    """Encode one MP4 with Blender's sequencer (main thread of a Blender process)."""
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
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Blender wrote no video to {out_path}")
    return out_path


def encode_jobs(jobs):
    for job in jobs:
        blender_encode(job["out_path"], job["fps"], [tuple(layer) for layer in job["layers"]], job["height"])


def start_background_encode(jobs, work_dir, blender_binary=None):
    """Run ``encode_jobs`` in a separate background Blender; returns (process, log_path)."""
    import bpy

    jobs_path = os.path.join(work_dir, "encode_jobs.json")
    with open(jobs_path, "w") as f:
        json.dump(jobs, f)
    log_path = os.path.join(work_dir, "encode.log")
    cmd = [
        blender_binary or bpy.app.binary_path,
        "-b",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        os.path.abspath(__file__),
        "--",
        jobs_path,
    ]
    flags = {}
    if sys.platform == "win32":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log_path, "w") as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
    return process, log_path


def log_tail(log_path, lines=4):
    try:
        with open(log_path, errors="replace") as f:
            return " | ".join(line.strip() for line in f.read().strip().splitlines()[-lines:])
    except OSError:
        return ""


if __name__ == "__main__":
    with open(sys.argv[sys.argv.index("--") + 1]) as f:
        encode_jobs(json.load(f))
