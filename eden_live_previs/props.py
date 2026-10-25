import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import live
from .session import get_session
from .safety import DEFAULT_PAID_SECONDS, MIN_PAID_SECONDS, MAX_PAID_SECONDS

DEFAULT_PROMPT = (
    "A sunlit ancient temple courtyard in a lush jungle, cinematic and realistic. Weathered stone pillars, "
    "moss-covered steps, carved statues and hanging vines. Golden hour light, soft haze. "
    "Keep the scene layout and camera movement. No text or HUD."
)


def _on_prompt(self, context):
    live.prompt_changed()


def _on_fps(self, context):
    get_session().fps = float(self.fps)


def _on_show_draft(self, context):
    get_session().side_by_side = self.show_draft


class EdenLiveRuntimeSettings(bpy.types.PropertyGroup):
    """Session choices belong to the running app, never to a shared .blend file."""
    backend_mode: EnumProperty(
        name="Mode", items=[("MOCK", "Offline test (free)", "Local repainting; no API calls"),
                            ("PAID", "Reactor (paid)", "Send drafts to EdenLive; requires confirmation")],
        default="MOCK", options={"SKIP_SAVE"},
    )
    paid_seconds: IntProperty(
        name="Attempt limit (seconds)", default=DEFAULT_PAID_SECONDS,
        min=MIN_PAID_SECONDS, max=MAX_PAID_SECONDS, options={"SKIP_SAVE"},
        description="Wall-clock limit including connection and GPU waiting; also requested as a server session cap",
    )
    preflight_summary: StringProperty(options={"SKIP_SAVE"})


class EdenLiveSettings(bpy.types.PropertyGroup):
    reference_path: StringProperty(
        name="Reference",
        description="Appearance reference (PNG/JPEG/WebP). Best: your opening camera frame restyled in an image tool, "
        "keeping the camera angle and object positions",
        subtype="FILE_PATH",
    )
    prompt: StringProperty(
        name="Prompt",
        description="Scene description. Edits are sent live with set_prompt and apply from the next chunk",
        default=DEFAULT_PROMPT,
        maxlen=4096,
        update=_on_prompt,
    )
    prompt_text: PointerProperty(
        name="Prompt Text",
        type=bpy.types.Text,
        description="Optional Text datablock used as the prompt instead of the field (handy for long prompts)",
    )
    seed: IntProperty(name="Seed", default=2026092901, min=0, max=2147483647)
    source_mode: EnumProperty(
        name="Draft view",
        items=[
            ("CAMERA", "Active camera", "Stream the scene camera's view at 1280x704"),
            ("VIEWPORT", "Viewport", "Stream the 3D Viewport's current view"),
        ],
        default="CAMERA",
    )
    fps: IntProperty(name="Stream FPS", default=20, min=10, max=60, update=_on_fps)
    hide_overlays: BoolProperty(name="Hide overlays", description="Capture without grid, gizmos and outlines", default=True)
    show_draft: BoolProperty(
        name="Draft side by side",
        description="Show the returned draft_video next to the generated main_video in the preview",
        default=False,
        update=_on_show_draft,
    )

    record_dir: StringProperty(
        name="Output",
        description="Folder for recorded MP4s. Empty: an eden_live_renders folder next to the .blend file "
        "(or in the temp folder while the file is unsaved)",
        subtype="DIR_PATH",
        default="",
    )
    record_name: StringProperty(name="Take", default="take")
    record_method: EnumProperty(
        name="Draft render",
        items=[
            ("VIEWPORT", "Viewport (fast)", "Draw the camera view with the 3D Viewport's shading"),
            ("RENDER", "Render engine", "Render each frame with the scene's engine (use Workbench or EEVEE)"),
        ],
        default="VIEWPORT",
    )
    record_tail: FloatProperty(
        name="Tail (s)", description="Keep streaming the last frame this long to collect delayed output", default=3.0,
        min=1.0, max=15.0,
    )
    record_keep_frames: BoolProperty(
        name="Keep PNG frames", description="Keep the PNG sequences next to the MP4s", default=False
    )


def get_settings(context=None):
    return (context or bpy.context).scene.eden_live


def prompt_value(settings):
    if settings.prompt_text is not None:
        return settings.prompt_text.as_string().strip()
    return settings.prompt.strip()
