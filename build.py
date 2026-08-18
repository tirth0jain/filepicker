"""Build FilePicker into a standalone Windows .exe with Nuitka.

Usage (Windows)::

    python build.py

Produces ``dist/FilePicker.exe`` (onefile, no console window, Python embedded
so no Python install is needed on target machines). The CI workflow calls this
on every commit and uploads the result to GitHub Releases.
"""

from __future__ import annotations

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
        # PyMuPDF compiles into a single huge C file (2.3M+ lines) that makes
        # MSVC run out of heap ("C1002") when several compilers run at once.
        # --low-memory forces ONE C-compile job so that file gets the full
        # memory budget. Slower, but it builds reliably.
        "--low-memory",
        "main.py",                           # the main module to compile
    ]

    # NOTE: Deliberately NOT using --remove-output (deletes the build dir and
    # is a known onefile troublemaker) and NOT setting --jobs (Nuitka defaults
    # to full CPU, which re-introduces the C1002 heap exhaustion).

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)
    print("Build complete. Binary is in dist/.")


if __name__ == "__main__":
    main()