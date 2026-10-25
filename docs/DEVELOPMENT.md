# Development and upstream relationship

This fork begins at upstream commit `b6146b32a805ca5aed0153f45d4bd359e61d4101` (October 22, 2026). It preserves the original MIT license and history. Upstream PRs #3 (one ZIP/release workflow) and #4 (Go Live panel) were still open when this branch began; neither is silently treated as merged code.

The extension has a separate ID, `eden_live_lab`, and version 0.2.0. The source package remains `eden_live_previs` to keep upstream comparisons practical. Its Blender operator names still overlap with upstream, so enable only one implementation in a Blender process.

## Code map

| Module | Responsibility |
|---|---|
| `safety.py` | Shared duration bounds and timeout constants. |
| `session.py` | Async session lifecycle, frame sender, preview conversion, watchdog, closure reporting. No bpy dependency. |
| `backends.py` | Reactor SDK adapter and local mock implementing the same command surface. |
| `diagnostics.py` | Redacted, local stage traces and reports. No telemetry. |
| `operators.py`, `props.py`, `prefs.py`, `ui.py` | Mode choice, per-attempt paid confirmation, free checks, Blender controls and export. |
| `capture.py`, `live.py` | Viewport/render capture, UI timers and preview updates. |
| `recorder.py`, `align.py`, `encode.py` | Pre-render, connect, stream, close, align, encode. |
| `tools/launch.ps1`, `tools/launch.py` | Windows checkout workflow with a fresh Blender session and offline default. |

The paid limit is copied into a backend at connection time, not read live from scene settings. A dedicated watchdog thread continues counting while the main UI is busy. Stop cancels pending work before closing; a failed SDK close retains its handle for a deliberate retry. The SDK receives a server duration request independently of the local watchdog.

## Build

```powershell
python .\tools\build_extension.py --blender 'D:\Apps\Blender\blender.exe'
```

Building needs Python, internet access to PyPI, and Blender. Running the free checkout test does not need a separate Python install. The build script downloads pinned SDK 1.11.0 wheels and verifies their SHA-256 against PyPI metadata, then uses Blender's extension build/validate commands. GitHub Actions produces platform ZIPs and installs/imports the Linux and Windows packages without creating paid sessions.

## Candidate next capabilities

These are possible follow-ups, not features already delivered:

1. Compare one existing environment across camera capture, a fixed restyled reference, and real EdenLive output.
2. Add reproducible scene/reference/prompt presets with explicit import/export and provenance.
3. Bank generated takes for local playback and mixing without reopening a model session.
4. Explore controls for visual phrases and camera changes once the baseline workflow is reliable.
5. Evaluate upstream's one-ZIP packaging and Go Live UX after their changes settle; preserve this fork's explicit paid gate and lifecycle bounds.

Keep experiments small and record observed outcomes separately from hypotheses. A mock result validates plumbing, not the model's visual performance.
