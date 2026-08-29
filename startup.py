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


def _is_frozen() -> bool:
    """True when running from a compiled (Nuitka) binary.

    Mirrors updater._is_frozen: checks the Nuitka markers AND the executable
    name, so a shortcut always points at FilePicker.exe — never at a
    ``pythonw.exe main.py`` pair (the regression the updater hit when
    ``sys.frozen`` was not set).
    """
    if getattr(sys, "frozen", False):
        return True
    if bool(getattr(sys, "nuitka_standalone", False)):
        return True
    if globals().get("__compiled__"):
        return True
    try:
        if Path(sys.executable).name.lower() == "filepicker.exe":
            return True
    except Exception:
        pass
    return False


def _ps_quote(value: str) -> str:
    """Escape a value for embedding inside a PowerShell single-quoted string."""
    return value.replace("'", "''")


def _target() -> tuple:
    """Return ``(target, args, working_dir)`` for the app.

    - Compiled binary: the .exe itself.
    - Dev mode: pythonw.exe (no console) with main.py as an argument.
    """
    if _is_frozen():
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    main_py = Path(__file__).resolve().parent / "main.py"
    return str(pythonw), f'"{main_py}"', str(main_py.parent)


def _powershell_flags() -> dict:
    """Return subprocess flags to hide the PowerShell console window on Windows."""
    flags: dict = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    # Also hide the window via PowerShell itself
    return flags


def install() -> bool:
    """Create the Startup-folder shortcut. Returns True on success."""
    lnk = _startup_dir() / _SHORTCUT_NAME
    target, args, workdir = _target()
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{_ps_quote(str(lnk))}'); "
        f"$s.TargetPath = '{_ps_quote(target)}'; "
        f"$s.Arguments = '{_ps_quote(args)}'; "
        f"$s.WorkingDirectory = '{_ps_quote(workdir)}'; "
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            check=True, capture_output=True, timeout=30,
            **_powershell_flags(),
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


def _read_shortcut(lnk: Path):
    """Return ``(target, arguments)`` of an existing .lnk, or None on failure."""
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{_ps_quote(str(lnk))}'); "
        "Write-Output $s.TargetPath; "
        "Write-Output $s.Arguments"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            check=True, capture_output=True, text=True, timeout=30,
            **_powershell_flags(),
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return None


def _same_path(a: str, b: str) -> bool:
    """Compare two Windows paths case-insensitively, ignoring slash style."""
    return a.strip().replace("/", "\\").lower() == b.strip().replace("/", "\\").lower()


def verify() -> bool:
    """Return True if auto-start will actually work at the next login.

    The Startup shortcut must exist, point at the currently running app (not a
    stale path from a previous install location), and its target file must
    still be present.
    """
    lnk = _startup_dir() / _SHORTCUT_NAME
    if not lnk.exists():
        return False
    target, _args, _workdir = _target()
    info = _read_shortcut(lnk)
    if info is None:
        return False
    lnk_target, _lnk_args = info
    if not _same_path(lnk_target, target):
        return False
    try:
        if not Path(lnk_target).exists():
            return False
    except OSError:
        return False
    return True


def ensure() -> bool:
    """Verify auto-start, reinstalling the shortcut if it is missing or stale.

    Returns True when auto-start is confirmed working afterwards.
    """
    if verify():
        return True
    return install()