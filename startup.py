"""Windows auto-start helper for FilePicker.

Two independent mechanisms are used, because on a real machine one of them is
always unavailable for some reason and "it silently does not start" is the
worst possible outcome:

* the per-user **Run key**
  (``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``) — written with
  :mod:`winreg`, so it needs no PowerShell, no COM object and no child
  process, and it is the mechanism Windows itself prefers. This is the
  primary one;
* the classic **Startup-folder shortcut** (``shell:startup``) — created with
  PowerShell's ``WScript.Shell``; it is what users recognise, can see with
  their own eyes, and can delete themselves.

Both are verified at every launch (:func:`verify`) and repaired when missing,
stale (pointing at an old install path) or broken (target no longer exists) —
an app that updates itself by replacing its own .exe must never end up with a
startup entry pointing at a path that no longer exists. :func:`ensure` is what
``main.py`` calls; :func:`state` explains, in the log, exactly what is in
place.

Usage (from the app)::

    python main.py --install-startup   # add to Windows startup
    python main.py --remove-startup    # remove from Windows startup
    python main.py --check-startup     # exit 0 when auto-start is working
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

try:  # Windows only — the dev machine and CI run on Linux.
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - exercised on non-Windows
    winreg = None  # type: ignore

_SHORTCUT_NAME = "FilePicker.lnk"

# Per-user Run key: starts the app for this user only (no admin rights) and is
# what Task Manager's "Startup apps" list shows.
_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "FilePicker"


def _is_windows() -> bool:
    """True when the Windows startup mechanisms are available.

    Tied to :mod:`winreg` being importable rather than to ``os.name``: that is
    exactly the condition the Run key needs, and it lets a test on a Linux dev
    box inject a fake ``winreg`` and exercise the real code path.
    """
    return winreg is not None


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


def _target() -> Tuple[str, str, str]:
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


def run_command() -> str:
    """The command line to register in the Run key (target + args, quoted)."""
    target, args, _workdir = _target()
    command = f'"{target}"'
    if args:
        command += f" {args}"
    return command


# ----------------------------------------------------------------------
# Run key (primary)
# ----------------------------------------------------------------------
def run_key_command() -> Optional[str]:
    """The command currently registered in the Run key (None when absent)."""
    if winreg is None or not _is_windows():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                            winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
        return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"[startup] could not read the Run key: {exc}")
        return None


def install_run_key() -> bool:
    """Point the per-user Run key at this app. Returns True on success."""
    if winreg is None or not _is_windows():
        return False
    command = run_command()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
        return True
    except OSError as exc:
        print(f"[startup] could not write the Run key: {exc}")
        return False


def remove_run_key() -> bool:
    """Delete the Run key value (True when it is gone afterwards)."""
    if winreg is None or not _is_windows():
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"[startup] could not remove the Run key: {exc}")
        return False


# ----------------------------------------------------------------------
# Startup-folder shortcut (secondary, visible to the user)
# ----------------------------------------------------------------------
def _powershell_flags() -> dict:
    """Return subprocess flags to hide the PowerShell console window."""
    flags: dict = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
    return flags


def install_shortcut() -> bool:
    """Create the Startup-folder shortcut. Returns True on success."""
    if not _is_windows():
        return False
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
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[startup] could not create the Startup shortcut: {exc}")
        return False


def remove_shortcut() -> bool:
    """Remove the Startup-folder shortcut (True when it is gone)."""
    lnk = _startup_dir() / _SHORTCUT_NAME
    try:
        if lnk.exists():
            lnk.unlink()
        return True
    except OSError as exc:
        print(f"[startup] could not remove the Startup shortcut: {exc}")
        return False


def _read_shortcut(lnk: Path) -> Optional[Tuple[str, str]]:
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


def shortcut_target() -> Optional[str]:
    """The target of the Startup shortcut (None when there is no shortcut)."""
    lnk = _startup_dir() / _SHORTCUT_NAME
    try:
        if not lnk.exists():
            return None
    except OSError:
        return None
    info = _read_shortcut(lnk)
    return info[0] if info else ""


# ----------------------------------------------------------------------
# Shared
# ----------------------------------------------------------------------
def _same_path(a: str, b: str) -> bool:
    """Compare two Windows paths case-insensitively, ignoring slash style."""
    return a.strip().replace("/", "\\").lower() == b.strip().replace("/", "\\").lower()


def _command_target(command: str) -> str:
    """The executable a Run-key command line starts (quotes stripped)."""
    command = str(command or "").strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end]
        return command.strip('"')
    return command.split(" ")[0] if command else ""


def _target_exists(path: str) -> bool:
    try:
        return bool(path) and Path(path).exists()
    except OSError:
        return False


def _run_key_ok(target: str) -> bool:
    command = run_key_command()
    if not command:
        return False
    return _same_path(_command_target(command), target) and _target_exists(target)


def _shortcut_ok(target: str) -> bool:
    lnk_target = shortcut_target()
    if not lnk_target:
        return False
    return _same_path(lnk_target, target) and _target_exists(target)


def is_installed() -> bool:
    """True when either startup mechanism is present (even if stale)."""
    if run_key_command():
        return True
    try:
        return (_startup_dir() / _SHORTCUT_NAME).exists()
    except OSError:
        return False


def verify() -> bool:
    """Return True if auto-start will actually work at the next login.

    A mechanism only counts when it points at the CURRENTLY running app (not a
    stale path from a previous install location) and its target still exists.
    """
    if not _is_windows():
        return False
    target, _args, _workdir = _target()
    return _run_key_ok(target) or _shortcut_ok(target)


def state() -> str:
    """A one-line description of what is registered, for the log."""
    target, _args, _workdir = _target()
    command = run_key_command()
    lnk_target = shortcut_target()
    bits = [f"target={target}"]
    bits.append(f"run-key={'yes' if command else 'no'}"
                + (f" ({command})" if command else ""))
    if lnk_target is None:
        bits.append("shortcut=no")
    else:
        bits.append(f"shortcut={lnk_target or 'unreadable'}")
    bits.append(f"working={'yes' if verify() else 'NO'}")
    return ", ".join(bits)


def install() -> bool:
    """Register BOTH mechanisms (each best-effort). True if either worked.

    The Run key is written first because it is the reliable one; the shortcut
    is then added so the user can see and manage the entry themselves. A
    failure of one is logged and does not stop the other.
    """
    if not _is_windows():
        return False
    run_ok = install_run_key()
    shortcut_ok = install_shortcut()
    if not run_ok and not shortcut_ok:
        print("[startup] auto-start could NOT be registered (Run key and "
              "Startup shortcut both failed)")
        return False
    return True


def remove() -> bool:
    """Remove both mechanisms. Returns True when neither remains."""
    if not _is_windows():
        return True
    run_ok = remove_run_key()
    shortcut_ok = remove_shortcut()
    return run_ok and shortcut_ok


def ensure() -> bool:
    """Verify auto-start, reinstalling it when missing or stale.

    Returns True when auto-start is confirmed working afterwards.
    """
    if verify():
        return True
    print(f"[startup] auto-start missing or stale — repairing ({state()})")
    install()
    ok = verify()
    print(f"[startup] auto-start {'repaired' if ok else 'repair FAILED'} "
          f"({state()})")
    return ok
