"""Build FilePicker into a standalone Windows app with Nuitka.

Usage (Windows)::

    python build.py

Produces ``dist/FilePicker-<version>-win64.zip`` containing a standalone app
folder (``FilePicker.exe`` + bundled libs, Python embedded, no Python install
needed on target machines). The CI workflow calls this on every commit and
uploads the zip to GitHub Releases.

Why standalone (a folder) instead of onefile (a single packed exe)?
Nuitka's onefile self-extracting bootstrap is exactly what heuristic antivirus
engines (Windows Defender / VirusTotal) flag as a false positive — a packed
binary that unpacks to temp and runs looks like a dropper. A standalone folder
has no self-extractor, which removes the main heuristic trigger.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

from version import VERSION


def main() -> None:
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--windows-console-mode=disable",   # no console window in the app
        "--enable-plugin=tk-inter",          # bundle tkinter
        "--include-package=customtkinter",   # already bundles its data files
        "--include-package=watchdog",
        "--include-package=PIL",
        "--include-package=pymupdf",
        "--include-package=openpyxl",
        "--include-package=xlrd",
        "--include-package=pystray",
        "--output-dir=dist",
        "--product-name=FilePicker",
        "--file-version=" + VERSION,
        "--product-version=" + VERSION,
        "--assume-yes-for-downloads",
        "main.py",                           # the main module to compile
    ]

    # NOTE: This command matches the FIRST build that succeeded (24m46s) on
    # Python 3.11 with PyMuPDF included. Do NOT add --jobs or --remove-output.
    # Do NOT switch to Python 3.13 — its generated C code for PyMuPDF overflows
    # MSVC's heap ("fatal error C1002"). The CI workflow pins Python 3.11.

    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd)

    # Nuitka standalone emits a "<name>.dist" folder under output-dir.
    dist_root = Path("dist")
    app_dirs = sorted(dist_root.glob("*.dist"))
    if not app_dirs:
        raise SystemExit("Build succeeded but no .dist folder was produced.")
    app_dir = app_dirs[-1]

    # Rename the produced exe (main.exe) to FilePicker.exe.
    exe = app_dir / "main.exe"
    if not exe.exists():
        candidates = list(app_dir.glob("*.exe"))
        if not candidates:
            raise SystemExit("No .exe found in the dist folder.")
        exe = candidates[0]
    exe.rename(app_dir / "FilePicker.exe")

    # Zip the app folder so it can be downloaded as one file and extracted.
    # Ship the current version tag as a marker file so the updater can record
    # which build is installed after a swap.
    marker = app_dir / "installed_version.txt"
    marker.write_text(f"v{VERSION}", encoding="utf-8")

    zip_path = dist_root / f"FilePicker-{VERSION}-win64.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(app_dir.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=f.relative_to(app_dir))

    print(f"Standalone build complete. ZIP: {zip_path}")


if __name__ == "__main__":
    main()