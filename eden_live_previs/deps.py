"""Locate, or as a fallback install, the Reactor Python SDK.

The platform-specific extension zips bundle the ``reactor-sdk`` wheel, which
Blender installs itself; nothing here runs in that case. When the add-on is
installed without wheels (legacy add-on zip, or from a git checkout), the
preferences offer an "Install Reactor SDK" button that runs Blender's own
Python ``-m pip install --target <user dir>``.
"""

import importlib
import os
import subprocess
import sys
import threading

import bpy

SDK_REQUIREMENT = "reactor-sdk>=1.11,<2"

_install = {"running": False, "log": "", "ok": None}


def deps_dir():
    try:
        return bpy.utils.extension_path_user(__package__, path="deps", create=True)
    except (ValueError, AttributeError, TypeError):
        base = bpy.utils.user_resource("SCRIPTS", path="eden_live_deps", create=True)
        path = os.path.join(base, f"py{sys.version_info.major}{sys.version_info.minor}")
        os.makedirs(path, exist_ok=True)
        return path


def add_deps_path():
    path = deps_dir()
    if path not in sys.path:
        sys.path.append(path)


def sdk_status():
    """(available, detail) without raising."""
    add_deps_path()
    try:
        module = importlib.import_module("reactor_sdk")
        return True, f"reactor-sdk {getattr(module, '__version__', '?')}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def install_state():
    return dict(_install)


def install_sdk_async():
    if _install["running"]:
        return
    _install.update(running=True, log="Installing reactor-sdk...", ok=None)
    target = deps_dir()

    def run():
        python = sys.executable
        flags = {}
        if sys.platform == "win32":
            flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            check = subprocess.run([python, "-m", "pip", "--version"], capture_output=True, text=True, **flags)
            if check.returncode != 0:
                subprocess.run([python, "-m", "ensurepip", "--user"], capture_output=True, text=True, **flags)
            cmd = [
                python, "-m", "pip", "install", "--upgrade", "--no-deps", "--only-binary=:all:",
                "--disable-pip-version-check", "--target", target, SDK_REQUIREMENT,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, **flags)
            ok = proc.returncode == 0
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
            _install.update(ok=ok, log="\n".join(tail) or ("Installed." if ok else "pip failed."))
        except Exception as exc:
            _install.update(ok=False, log=f"{type(exc).__name__}: {exc}")
        finally:
            importlib.invalidate_caches()
            _install["running"] = False

    threading.Thread(target=run, name="EdenLivePip", daemon=True).start()
