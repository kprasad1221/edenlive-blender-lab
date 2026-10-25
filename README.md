# EdenLive Blender Lab

A small testbed for turning Blender scenes into live visual prototypes with Reactor's EdenLive model. Start with a **free offline simulation**, inspect the capture and recording pipeline, then deliberately open a short paid session when the local checks pass.

Built by [Pixel Rhythms / KP](https://github.com/kprasad1221) on [willemhelmet's EdenLive Pre-vis integration](https://github.com/willemhelmet/Eden-live-blender-reactor). This is an independent experimental fork, not an official Reactor product. Upstream history and the MIT license are preserved.

**Development branch:** `codex/windows-testbench` · **Blender:** 4.2+ · **First target:** Windows x64 · **Extension version:** 0.2.0

## What is in this first prototype?

- An explicit **Offline test (free)** mode, selected by default. It repaints the draft locally; it does not call the EdenLive model.
- **Check Inputs (free)** for the camera, reference, prompt, Blender version, and paid-mode prerequisites.
- The existing camera/viewport capture, reference-image workflow, live prompt editing, split preview, and timeline-to-MP4 recording.
- A confirmation for **each paid attempt**, a visible countdown, and a **30-second default limit** including connection waiting. The UI permits 10–120 seconds; zero and unbounded sessions are rejected.
- A local watchdog plus the SDK's `max_session_duration_seconds` request to Reactor. Stop, connection errors, first-frame timeouts, output stalls, and recording cancellation all request closure. A failed close stays visible and can be retried.
- Recording renders its drafts **before opening a session** and closes the session **before encoding MP4s**.
- A Log panel, local JSONL diagnostics, and **Export Debug Report** with stages, redacted tracebacks, elapsed time, frame counts, and closure status.

No paid sessions run in CI. Mock output demonstrates the integration, not EdenLive image quality. Paid service access and real Windows GPU viewport behavior still need a deliberate test on the target machine. See [validation scope](docs/TESTING.md).

## Clone and run the free Windows test

Install [Git for Windows](https://git-scm.com/download/win) if `git` is unavailable. In PowerShell:

```powershell
cd D:\Projects
git clone --branch codex/windows-testbench https://github.com/kprasad1221/edenlive-blender-lab.git
cd edenlive-blender-lab
.\tools\launch.ps1
```

The launcher finds Blender on PATH or in the standard `Program Files\Blender Foundation` location. For a custom installation:

```powershell
.\tools\launch.ps1 -Blender 'D:\Apps\Blender\blender.exe'
```

Use the actual path on your machine. If Windows blocks a downloaded script, review the files and use `Unblock-File .\tools\launch.ps1` for that file; an organization-managed execution policy may require an approved launch method. The equivalent command, without the PowerShell launcher, is:

```powershell
$env:EDEN_LIVE_MOCK = '1'
& 'D:\Apps\Blender\blender.exe' --factory-startup --offline-mode --python .\tools\launch.py
```

The launcher uses a fresh Blender session and loads this checkout without installing over your existing add-ons. With no scene argument it creates a disposable demo scene with a 48-frame range. Opening the app never opens a Reactor session. A Python installation, SDK, and API key are not needed for this offline path; Blender supplies Python and NumPy.

1. Open **N → EdenLive**. Confirm **Offline test (free)** / **Mock (offline)**.
2. In **Look**, click **Capture**. Keep the supplied prompt for the first test.
3. Click **Check Inputs (free)**. Read any failures in **Log**.
4. Click **Connect (offline)**, then **Start → Preview: Split**. Move the camera or scrub a few frames. Expect a stylized repaint with roughly one second of simulated latency.
5. Click **Stop** and wait for `disconnected`.
6. Optionally click **Record Timeline** while disconnected. The 48-frame demo produces local MP4s and a report. Output defaults to a temporary folder for an unsaved scene; use **Output** to choose a persistent folder on D:.
7. **Log → Export Debug Report** saves a local report for troubleshooting.

**First success criterion:** your camera changes reach the preview, Stop works, and a short offline recording produces a playable MP4. No credits are needed.

## Use an existing environment

Open a saved **copy** of the scene:

```powershell
.\tools\launch.ps1 -Blender 'D:\Apps\Blender\blender.exe' -Scene 'D:\Projects\MyScene\edenlive-test.blend'
```

With `-Scene`, the launcher does not build the demo or replace scene content. Use one camera, Solid shading, and a short shot first. Save changes only to the test copy. On loading another `.blend`, any session is stopped and mode returns to offline. Scene files contain scene settings, never the API key or a paid confirmation.

## Install as an extension instead

Use the artifact from a successful [Actions run on this branch](https://github.com/kprasad1221/edenlive-blender-lab/actions). Download **edenlive-lab-all-packages**, extract the outer archive, and install the inner **`eden_live_lab-0.2.0-windows_x64.zip`** through **Edit → Preferences → Get Extensions → dropdown → Install from Disk**. Enable **EdenLive Lab**.

Disable the upstream EdenLive add-on before enabling this fork: their operator names overlap. Keep the Windows ZIP compressed when installing. The source-code ZIP is not the extension installer. The extension bundles Reactor SDK 1.11.0; the checkout launcher offers the pinned SDK installer only when needed for paid work.

## Deliberate paid test

Follow [the paid-test guide](docs/PAID_TEST.md) after the offline test passes. Paid mode is available, but the repository does not claim a live service test has passed.

**Pause does not stop billing.** Stop requests session termination. The local countdown bounds the attempt from connection initiation; the server cap bounds a session from its creation. These are time controls, not a guaranteed dollar limit. Network, process, SDK, or service failures can prevent local confirmation of closure. Always check Reactor's dashboard after a paid test, especially if the UI says closure is unconfirmed.

## Development

- [Testing and validation scope](docs/TESTING.md)
- [Diagnostics and troubleshooting](docs/DEBUGGING.md)
- [Architecture, upstream relationship, and next capabilities](docs/DEVELOPMENT.md)
- [MIT license](LICENSE)

Keep API keys, personal scenes, debug exports, and rendered takes out of commits. Bundled Reactor SDK code carries its own Apache-2.0 license. EdenLive is a hosted model accessed through Reactor; its service terms and charges apply to paid sessions.
