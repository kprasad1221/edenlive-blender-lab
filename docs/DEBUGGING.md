# Diagnostics and troubleshooting

## Start with a free check

**Check Inputs (free)** validates local requirements. In offline mode the SDK, key, and network access are unnecessary. In paid mode it checks SDK import, whether a key is present, online-access permission, and use of the official Reactor API endpoint. It never opens a Reactor session. A passing preflight does not prove account access, model entitlement, or network connectivity.

## Collect evidence

The sidebar's **Log** shows the latest stages. **Export Debug Report** writes a local JSON report containing:

- OS, architecture, Blender and Python versions;
- mode, attempt ID, local elapsed time and configured time limit;
- connection status, close result, and stop reason;
- received frame counts and capture timing;
- timestamped stages and redacted exception tracebacks.

Each attempt also writes a bounded diagnostic trace under the operating system's temporary `edenlive-lab` directory. If that directory is not writable, the in-memory report remains available. There is no telemetry or automatic report upload.

Keys, bearer tokens, URLs, known prompts, reference paths, and common user-file paths are redacted. Never treat redaction as permission to publish: review exports before sharing. MP4 recording reports intentionally contain the prompt and seed for reproducibility; those are local take artifacts, separate from the redacted debug export. `.gitignore` excludes keys, debug traces, takes, and `.blend` files.

## Useful failure distinctions

| Observation | Meaning / next step |
|---|---|
| No EdenLive tab | Confirm Blender 4.2+, extension enabled, and the original upstream add-on disabled. |
| Offline selected; SDK missing | Offline work can continue. The SDK is only needed for paid calls. |
| Reference check fails | Click Capture with the 3D Viewport visible, or select a readable PNG/JPEG/WebP at least 32×32. |
| Black preview | Confirm an active camera, successful capture, a prompt, and Start. Use Solid shading. |
| Connection timed out | Allocation or networking did not complete within the bound. Inspect the report; no automatic retry occurs. |
| First frame timeout | Connection was opened but no output arrived after Start. The session is asked to close. |
| Closure unconfirmed | Retry Stop, then inspect the Reactor dashboard. The client does not claim billing has stopped. |
| Recording cancelled / failed | The session is asked to close. Received PNG frames are preserved where available. |
| Encode failed | Encoding happens after session closure. Inspect retained frames and `encode.log`; do not open another session merely to encode. |

An issue report should state the branch/commit, mode, Blender version, the last successful stage, and the smallest steps to reproduce. Attach the reviewed debug export only when useful. Do not attach an API key, private scene, or unpublished artwork without intending to make it public.
