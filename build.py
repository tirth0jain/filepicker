"""Build FilePicker into a standalone Windows .exe with Nuitka.

Usage (Windows)::

    python build.py

Produces ``dist/FilePicker.exe`` (onefile, no console window, Python embedded
so no Python install is needed on target machines). The CI workflow calls this
on every commit and uploads the result to GitHub Releases.
"""

from __future__ import annotations

import multiprocessing
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
        "main.py",                           # the main module to compile
    ]

    # Parallel C compilation (capped to avoid memory blow-ups).
    jobs = min(multiprocessing.cpu_count(), 4)
    cmd.append(f"--jobs={jobs}")

    # NOTE: Nuitka has no --cache-dir option; its cache lives at a fixed
    # location (%LOCALAPPDATA%\Nuitka on Windows, ~/.cache/Nuitka elsewhere).
    # The CI workflow caches that directory so rebuilds reuse compiled objects.

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print("Build complete. Binary is in dist/.")


if __name__ == "__main__":
    main()