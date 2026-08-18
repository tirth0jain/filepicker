"""Windows auto-start helper for FilePicker.

Creates or removes a shortcut in the user's Startup folder so the app launches
automatically when Windows starts. Uses PowerShell's WScript.Shell COM object,
so no extra dependencies are needed.

Usage (from the app)::

    python main.py --install-startup   # add to Windows startup
    python main.py --remove-startup    # remove from Windows startup
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SHORTCUT_NAME = "FilePicker.lnk"


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def _target() -> tuple:
    """Return ``(target, args, working_dir)`` for the app.

    - Compiled binary: the .exe itself.
    - Dev mode: pythonw.exe (no console) with main.py as an argument.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    main_py = Path(__file__).resolve().parent / "main.py"
    return str(pythonw), f'"{main_py}"', str(main_py.parent)


def install() -> bool:
    """Create the Startup-folder shortcut. Returns True on success."""
    lnk = _startup_dir() / _SHORTCUT_NAME
    target, args, workdir = _target()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{args}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True, capture_output=True, timeout=30,
        )
        return lnk.exists()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def remove() -> bool:
    """Remove the Startup-folder shortcut. Returns True on success."""
    lnk = _startup_dir() / _SHORTCUT_NAME
    try:
        if lnk.exists():
            lnk.unlink()
        return True
    except OSError:
        return False


def is_installed() -> bool:
    """Return True if the Startup-folder shortcut exists."""
    return (_startup_dir() / _SHORTCUT_NAME).exists()