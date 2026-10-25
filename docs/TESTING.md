# Testing and validation scope

All default tests are free and use mock/fake backends. No CI job receives a Reactor key or runs the paid smoke test.

## Fast lifecycle tests

```powershell
python -m pip install 'numpy>=1.26,<3'
python -m unittest discover -s tests -p test_safety.py -v
```

These cover total duration while idle/paused, connection timeout, Stop during connection, late-ready prevention, failed-command cleanup, first-frame and stream-stall timeouts, retryable failed closure, repeated Stop, SDK duration configuration, rejected unbounded limits, redaction, and offline operation without the SDK.

## Blender tests

```powershell
& 'D:\Apps\Blender\blender.exe' -b --factory-startup --python-exit-code 1 --python .\tests\test_blender_guards.py
& 'D:\Apps\Blender\blender.exe' -b --factory-startup --python-exit-code 1 --python .\tests\test_headless.py
```

The guard test exercises offline defaults, the explicit paid gate, free preflight, debug export, and reset on file loading. The headless suite covers registration, pixel conversion, camera capture, mock commands, alignment, both MP4 encoders, encoding fallback, and preservation of failed takes. A display-dependent `tests/test_ui.py` also exercises the actual split-preview and recording UI using the mock backend.

## Evidence for this first implementation

Local validation used **Blender 4.2.3 on Linux**:

- 13 lifecycle/redaction/backend tests passed.
- Blender integration guard checks passed.
- 12/12 headless checks passed, including camera capture and mock MP4 recording.

The public CI workflow additionally runs lifecycle tests on Windows and Linux and installs the built extension into clean Blender user directories on both platforms. Inspect the specific branch commit's Actions result before treating those remote checks as passed.

## Still requires target-machine / live verification

- Interactive viewport capture on the user's Windows GPU and existing scenes.
- The Windows checkout launcher on that machine, especially a custom Blender installation path.
- Reactor credentials, model access, allocation latency, actual generated quality and final billing.
- Real server enforcement of the requested session bound and real disconnect acknowledgements.

No successful paid run is claimed. Public demonstrations should identify mock footage as a local simulation. A paid test is deliberately separate; see [PAID_TEST.md](PAID_TEST.md).
