#!/usr/bin/env python3
"""Build installable zips.

    python tools/build_extension.py --blender /path/to/blender

1. Downloads the reactor-sdk wheels listed in blender_manifest.toml from PyPI
   (sha256-verified) into eden_live_previs/wheels/.
2. Runs ``blender --command extension build --split-platforms`` -> one extension
   zip per platform in dist/, each bundling only its own wheel.
3. Writes dist/eden_live_previs-<version>-legacy-addon.zip: the add-on without
   wheels, for "Install from Disk" as a legacy add-on (SDK installed from prefs).

Use --skip-blender to only fetch wheels and build the legacy zip.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "eden_live_previs")
MANIFEST = os.path.join(ADDON, "blender_manifest.toml")
DIST = os.path.join(ROOT, "dist")


def manifest_wheels():
    text = open(MANIFEST, encoding="utf-8").read()
    return re.findall(r'"\./wheels/([^"]+\.whl)"', text)


def manifest_version():
    return re.search(r'^version\s*=\s*"([^"]+)"', open(MANIFEST, encoding="utf-8").read(), re.M).group(1)


def fetch_wheels():
    wanted = manifest_wheels()
    versions = {re.match(r"reactor_sdk-([^-]+)-", w).group(1) for w in wanted}
    if len(versions) != 1:
        sys.exit(f"manifest lists mixed reactor-sdk versions: {versions}")
    version = versions.pop()
    with urllib.request.urlopen(f"https://pypi.org/pypi/reactor-sdk/{version}/json") as resp:
        files = {f["filename"]: f for f in json.load(resp)["urls"]}
    out_dir = os.path.join(ADDON, "wheels")
    os.makedirs(out_dir, exist_ok=True)
    for name in wanted:
        if name not in files:
            sys.exit(f"{name} is not published on PyPI")
        info = files[name]
        path = os.path.join(out_dir, name)
        if os.path.exists(path) and hashlib.sha256(open(path, "rb").read()).hexdigest() == info["digests"]["sha256"]:
            print(f"ok      {name}")
            continue
        print(f"fetch   {name}")
        with urllib.request.urlopen(info["url"]) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != info["digests"]["sha256"]:
            sys.exit(f"sha256 mismatch for {name}")
        with open(path, "wb") as f:
            f.write(data)


def build_extensions(blender):
    os.makedirs(DIST, exist_ok=True)
    cmd = [blender, "--factory-startup", "--command", "extension", "build", "--split-platforms",
           "--source-dir", ADDON, "--output-dir", DIST]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    for name in sorted(os.listdir(DIST)):
        if name.endswith(".zip") and "legacy" not in name:
            subprocess.run([blender, "--factory-startup", "--command", "extension", "validate",
                            os.path.join(DIST, name)], check=True)


def build_legacy():
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, f"eden_live_previs-{manifest_version()}-legacy-addon.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(ADDON):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "wheels")]
            for name in files:
                if name.endswith((".pyc", ".whl")):
                    continue
                full = os.path.join(base, name)
                zf.write(full, os.path.join("eden_live_previs", os.path.relpath(full, ADDON)))
    print(f"wrote   {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blender", default=shutil.which("blender") or "blender")
    parser.add_argument("--skip-blender", action="store_true")
    args = parser.parse_args()
    fetch_wheels()
    if not args.skip_blender:
        build_extensions(args.blender)
    build_legacy()


if __name__ == "__main__":
    main()
