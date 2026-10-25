# One deliberate paid test

Run the offline test first. This guide describes an implemented path, not evidence of a successful paid session.

## Prepare while disconnected

1. Open a test copy of your scene. Use one camera and a two-second shot (48 frames at 24 fps).
2. Capture the opening view. Use that capture for a basic connection test, or restyle it while preserving geometry and camera composition to test the model's visual transformation.
3. Set a short prompt and choose an output folder. Keep Solid shading initially.
4. Install the bundled Windows extension, or use **Preferences → Add-ons → EdenLive Lab → Install Reactor SDK** for a checkout launch. That installs pinned `reactor-sdk==1.11.0` using Blender's Python. Downloading the SDK uses internet access but does not create a model session.

If you started with the offline launcher, close it and reopen with:

```powershell
.\tools\launch.ps1 -Mode Paid -Blender 'D:\Apps\Blender\blender.exe' -Scene 'D:\Projects\MyScene\edenlive-test.blend'
```

This opens the interface in paid mode; it does **not** connect. The offline launcher sets a force-offline environment flag, so changing the mode control inside that particular process cannot enable paid calls.

## Open one session

1. Enable **Preferences → System → Network → Allow Online Access**.
2. Enter your key in Blender's password field. It lives in user preferences, not the scene. The checkout launcher uses factory startup: enter the key for that test session or supply an existing `REACTOR_API_KEY` environment variable. Do not put a key in scripts, screenshots, issues, or GitHub Secrets for this test suite.
3. Choose **Reactor (paid)**, keep the **30-second** limit, and run **Check Inputs (free)**.
4. For interactive exploration, click **Connect (paid)…** and acknowledge the credit use. Once ready, click **Start** immediately; connection waiting counts against the local timer. The limit cannot be extended while a session is open.
5. Check the preview briefly and click **Stop**. Check the dashboard for final session state and usage.

If connection allocation is too slow for 30 seconds, the test should stop. Inspect the report before deciding whether another attempt with a larger explicit limit is worthwhile. There are no automatic paid retries.

## Record one shot

Stop the live preview and confirm `disconnected`, then choose **Record Timeline** and acknowledge that attempt. Drafts render locally first. Only when they are ready does the paid connection open. After streaming the short shot and its tail, the session closes before local alignment and MP4 encoding. Cancel, errors, and the attempt watchdog also request closure. Retried takes use unique output names.

## What the limits do

| Control | Behavior |
|---|---|
| Explicit paid confirmation | Required for each UI Connect or Record attempt. Mode selection or an existing key does not connect. |
| Local attempt timer | Starts before SDK connection; includes authentication, GPU waiting, Start delay, streaming, and pauses. |
| Server duration request | Passes the same bound as `max_session_duration_seconds` to SDK 1.11.0's scoped-token flow. |
| Connect timeout | 20 seconds, also subject to the total attempt limit. |
| First output timeout | 15 seconds after Start, also subject to the total limit. |
| Output stall | Requests closure after 10 seconds without output while a world is running. |
| Close timeout | After 5 seconds without successful SDK disconnect, reports **closure unconfirmed** and retains the handle for Stop retry. |
| Allowed paid limit | Default 30 seconds, adjustable 10–120 seconds before an attempt. Cannot be disabled. |

A server cap is requested, not locally verified against billing. Crashes, native-library stalls, network loss, or service behavior remain outside a client-side guarantee. A paused or failed UI is not proof that a paid session has closed.

## Optional developer smoke test

`tests/test_live_reactor.py` refuses to connect unless `EDEN_ALLOW_PAID` is exactly `I_ACCEPT_REACTOR_CHARGES`. It also requires `REACTOR_API_KEY`, Blender online access, an importable SDK, and no force-offline flag. It pre-renders 48 demo frames and opens one bounded session. CI never invokes it. Prefer the UI for the first paid run so the countdown and Stop control are visible.
