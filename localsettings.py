"""Per-person settings for a SHARED FilePicker install.

FilePicker can be installed ONCE in a server folder and run by everybody from
that same folder (everyone double-clicks ``FilePicker.exe`` on the share). In
that setup ``config.json`` next to the exe is **shared** — the catalog (clients,
sites, mappings, materials, doc types) must be identical for everyone, so it
belongs in one file. But a few settings are about the PERSON or the MACHINE and
*must* differ between people:

    watch_directory      where THIS person's scans/downloads land
    root_directory       where THIS person's sorted tree lives
    popup_delay_seconds  how long a finished file waits before the popup
    enable_ocr + ocr_model / ocr_api_base / ocr_thinking
    enable_live_config / enable_github_push
    auto_start           whether THIS machine launches it at login

The shared file can only hold one value for those, so each person's own value
lives here instead — in a per-Windows-user file **outside** the shared folder::

    %LOCALAPPDATA%\\FilePicker\\settings.json      (Windows)
    ~/.config/filepicker/settings.json             (dev / Linux)

A value in this file OVERRIDES the shared config.json for that person. That is
what makes "each person has their own watch folder" work with a single install,
and it is also why the shared file is never rewritten by a normal popup (two
people saving at once can no longer clobber each other's catalog edits).

The same idea solves the second half of the problem — *"the popup only opens
for the person who dropped the file"*: because each person watches their own
folder, only their own running copy sees the drop. :class:`UserRegistry` adds a
belt-and-braces guard for the messy case (somebody left on the shared default
folder that contains a colleague's folder): every running copy publishes its own
watch folder into ``<shared folder>/users/<user>@<machine>.json`` — one file per
person, so there is never a write conflict — and a copy that watches a *wider*
folder ignores files that sit inside a colleague's narrower one.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Folder name used under %LOCALAPPDATA% (Windows) / ~/.config (dev).
APP_DIR_NAME = "FilePicker"

# Values of FILEPICKER_SHARED that mean "yes, this is a shared install".
_TRUE_VALUES = {"1", "true", "yes", "on", "shared"}


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------
def app_directory() -> Path:
    """The folder FilePicker runs from — the exe's folder when compiled.

    In a Nuitka standalone build the modules live inside the distribution but
    ``__file__`` can point at an embedded location, so the running executable is
    the only reliable anchor. Matches ``config.default_config_path()``.
    """
    exe = Path(sys.executable)
    if (getattr(sys, "frozen", False)
            or bool(getattr(sys, "nuitka_standalone", False))
            or exe.name.lower() == "filepicker.exe"):
        return exe.resolve().parent
    return Path(__file__).resolve().parent


def settings_directory() -> Path:
    """Per-Windows-user folder for this machine's FilePicker settings.

    ``%LOCALAPPDATA%`` (not ``%APPDATA%``) on purpose: the watch folder is a
    path on *this* PC (a mapped drive letter, a scanner folder), so it must not
    roam to another machine with a roaming profile.
    """
    override = os.environ.get("FILEPICKER_SETTINGS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA", "").strip()
                or os.environ.get("APPDATA", "").strip())
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "filepicker"


def local_settings_path() -> Path:
    """The per-person settings file (``FILEPICKER_SETTINGS`` overrides it)."""
    override = os.environ.get("FILEPICKER_SETTINGS", "").strip()
    if override:
        return Path(override).expanduser()
    return settings_directory() / "settings.json"


def log_path() -> Path:
    """Where FilePicker.log goes on a shared install (per person, not shared).

    One log file in the server folder would interleave every machine's lines and
    would need write access for everybody; each person's own log is both easier
    to read and one less reason to write into the shared folder at all.
    """
    return settings_directory() / "FilePicker.log"


# --------------------------------------------------------------------------
# Who is running this copy
# --------------------------------------------------------------------------
def current_user() -> str:
    """The Windows account name running this copy ('' when unknown)."""
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        import getpass
        return getpass.getuser().strip()
    except Exception:
        return ""


def current_machine() -> str:
    """This PC's name ('' when unknown)."""
    for key in ("COMPUTERNAME", "HOSTNAME"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        return socket.gethostname().strip()
    except Exception:
        return ""


def user_keys() -> List[str]:
    """Every spelling that identifies this person, lowercased.

    A shared config can pre-assign folders in ``watch_directories`` and the
    admin may write them as ``manish``, ``manish@pc-01`` or ``PC-01\\manish``;
    all three are matched so nobody has to guess the exact key.
    """
    user = current_user().lower()
    machine = current_machine().lower()
    keys: List[str] = []
    for key in (f"{user}@{machine}" if user and machine else "",
                user, machine,
                f"{machine}\\{user}" if user and machine else ""):
        if key and key not in keys:
            keys.append(key)
    return keys


def user_key() -> str:
    """This person's registry key: ``user@machine`` (falls back sensibly)."""
    user = current_user() or "user"
    machine = current_machine() or "pc"
    return f"{user}@{machine}".lower()


# --------------------------------------------------------------------------
# Is this a shared install?
# --------------------------------------------------------------------------
def is_network_path(path: Any) -> bool:
    """True when *path* lives on a network share (UNC or a mapped drive).

    Used to recognise "the exe was started from the server folder" without
    anybody having to configure anything: a mapped drive (``Z:\\FilePicker``)
    reports DRIVE_REMOTE, and a UNC path (``\\\\server\\share\\FilePicker``) is
    remote by definition.
    """
    text = str(path or "")
    if not text:
        return False
    if text.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(text))[0]
        if not drive:
            return False
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def _flag_from_config(config_path: Path) -> Optional[bool]:
    """The ``shared_install`` flag in a config.json, or None when not set."""
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("shared_install"), bool):
        return bool(data["shared_install"])
    return None


def install_is_shared(app_dir: Optional[Path] = None,
                      config_flag: Optional[bool] = None,
                      config_path: Optional[Path] = None) -> bool:
    """Whether this copy runs from a SHARED install.

    Order of precedence:

    1. ``FILEPICKER_SHARED`` (1/0) — an explicit switch, used by tests and by
       anybody who wants to force the mode on a local folder;
    2. ``"shared_install": true/false`` in config.json — the admin's switch,
       and the one to use when the server folder is reached through a path
       Windows does not report as remote;
    3. the app folder being a network path — the automatic case (running the
       exe straight off the server share or a mapped drive).
    """
    env = os.environ.get("FILEPICKER_SHARED", "")
    if env is not None and env.strip():
        return env.strip().lower() in _TRUE_VALUES
    if isinstance(config_flag, bool):
        return config_flag
    if config_path is None and app_dir is not None:
        config_path = Path(app_dir) / "config.json"
    if config_path is not None:
        flag = _flag_from_config(Path(config_path))
        if flag is not None:
            return flag
    return is_network_path(app_dir) if app_dir is not None else False


# --------------------------------------------------------------------------
# The per-person settings file
# --------------------------------------------------------------------------
class LocalSettings:
    """Thread-safe JSON store for the per-person settings (see module docstring).

    Every read is cheap (the file is tiny and cached in memory) and every write
    is atomic, so a half-written file can never make the app fall back to the
    shared folder's value by accident.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else local_settings_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._loaded = False

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if not self._loaded:
                self._data = self._read()
                self._loaded = True
            return self._data

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        """The per-person value for *key* (``default`` when it was never set).

        An empty string counts as "not set": clearing the watch folder in the
        settings file must not leave the app watching nothing.
        """
        value = self.load().get(key, default)
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return value

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def set(self, key: str, value: Any) -> None:
        """Store *key* for this person and write the file immediately."""
        with self._lock:
            self.load()
            if self._data.get(key) == value:
                return
            self._data[key] = value
            self._save()

    def update(self, values: Dict[str, Any]) -> None:
        with self._lock:
            self.load()
            changed = False
            for key, value in values.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed = True
            if changed:
                self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[settings] could not write {self.path}: {exc}")


# --------------------------------------------------------------------------
# Who is watching what (shared folder registry)
# --------------------------------------------------------------------------
class UserRegistry:
    """``<shared folder>/users/<user>@<machine>.json`` — one file per person.

    Each running copy publishes the folder it watches, and reads the others so a
    copy that watches a WIDER folder (the shared default, a parent folder) can
    leave a colleague's files alone — the popup then only opens on the person
    who dropped the file. One file per person means no two copies ever write the
    same file, so nothing needs locking.
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = (Path(directory) if directory
                          else app_directory() / "users")

    def publish(self, watch_directory: str, root_directory: str = "",
                version: str = "") -> Optional[Path]:
        """Record this person's watch folder (best effort, never raises)."""
        watch = str(watch_directory or "").strip()
        if not watch:
            return None
        entry = {
            "user": current_user(),
            "machine": current_machine(),
            "watch_directory": watch,
            "root_directory": str(root_directory or "").strip(),
            "version": version,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self.directory / f"{user_key()}.json"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, path)
            return path
        except OSError as exc:
            print(f"[settings] could not publish this machine's watch folder: {exc}")
            return None

    def entries(self, include_self: bool = False) -> List[Dict[str, Any]]:
        """Every published entry (other people's by default)."""
        out: List[Dict[str, Any]] = []
        try:
            files = sorted(self.directory.glob("*.json"))
        except OSError:
            return out
        mine = user_key()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    entry = json.load(fh)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict) or not str(entry.get("watch_directory", "")).strip():
                continue
            entry["_key"] = path.stem.lower()
            if not include_self and entry["_key"] == mine:
                continue
            out.append(entry)
        return out


# --------------------------------------------------------------------------
# Path helpers for the ownership guard
# --------------------------------------------------------------------------
def norm_path(path: Any) -> str:
    """Comparable form of a path (case/separator insensitive on Windows)."""
    text = str(path or "").strip().rstrip("\\/")
    if not text:
        return ""
    try:
        text = os.path.abspath(text)
    except Exception:
        pass
    return os.path.normcase(text)


def is_inside(path: Any, folder: Any) -> bool:
    """True when *path* is *folder* itself or lives under it."""
    child, parent = norm_path(path), norm_path(folder)
    if not child or not parent:
        return False
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def foreign_owner(path: Any, my_watch: Any,
                  entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The colleague whose own watch folder *path* belongs to, if any.

    Only a folder NARROWER than ours counts: if we watch ``Z:/Unsorted`` and a
    colleague watches ``Z:/Unsorted/Nitin``, a file in Nitin's folder is his
    (his popup opens, ours does not). A colleague watching a *wider* folder
    never steals our files — whoever is more specific wins.
    """
    mine = norm_path(my_watch)
    for entry in entries:
        theirs = norm_path(entry.get("watch_directory"))
        if not theirs or theirs == mine:
            continue
        if is_inside(path, theirs) and len(theirs) > len(mine):
            return entry
    return None


def same_folder_users(my_watch: Any,
                      entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Colleagues watching exactly the same folder as us (they get a warning)."""
    mine = norm_path(my_watch)
    if not mine:
        return []
    return [e for e in entries if norm_path(e.get("watch_directory")) == mine]


def assigned_watch_folder(mapping: Any) -> Optional[str]:
    """The folder an admin pre-assigned to this person in the shared config.

    ``config.json`` may carry::

        "watch_directories": {"manish": "Z:/Unsorted/Manish",
                              "nitin@pc-02": "Z:/Unsorted/Nitin"}

    Keys are matched against every spelling of this person (see
    :func:`user_keys`), so nobody has to type their own name anywhere: the app
    picks up its folder from the shared file and starts watching immediately.
    """
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for key in user_keys():
        value = lowered.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
