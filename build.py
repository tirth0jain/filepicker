"""Build FilePicker into a standalone Windows .exe with Nuitka.

Usage (Windows)::

    python build.py

Produces ``dist/FilePicker.exe`` (onefile, no console window, Python embedded
so no Python install is needed on target machines). The CI workflow calls this
on every commit and uploads the result to GitHub Releases.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys

from version import VERSION


def main() -> None:
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--onefile",
        "--windows-console-mode=disable",   # no console window in the app
        "--enable-plugin=tk-inter",          # bundle tkinter
        "--include-package=customtkinter",   # already bundles its data files
        "--include-package=watchdog",
        "--include-package=PIL",
        "--include-package=pymupdf",
        "--include-package=openpyxl",
        "--include-package=xlrd",
        "--output-dir=dist",
        "--product-name=FilePicker",
        "--file-version=" + VERSION,
        "--product-version=" + VERSION,
        "--assume-yes-for-downloads",
        "--remove-output",                   # clean intermediate build files
    ]

    # Parallel C compilation (capped to avoid memory blow-ups).
    jobs = min(multiprocessing.cpu_count(), 4)
    cmd.append(f"--jobs={jobs}")

    # Reuse a persistent Nuitka cache when NUITKA_CACHE_DIR is set (CI uses
    # this with a GitHub Actions cache so rebuilds are much faster).
    cache_dir = os.environ.get("NUITKA_CACHE_DIR")
    if cache_dir:
        cmd.append("--cache-dir=" + cache_dir)

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print("Build complete. Binary is in dist/.")


if __name__ == "__main__":
    main()