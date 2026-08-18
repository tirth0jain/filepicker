"""Self-update support for FilePicker.

Checks GitHub Releases for a newer binary and, if found, downloads it and
performs an atomic swap so the running ``.exe`` can be replaced on Windows.

The update server is GitHub Releases: the CI workflow (``.github/workflows/
build.yml``) compiles the app with Nuitka on every commit and uploads the
``.exe`` as a release asset tagged ``v<version>-<commit-sha>``. The app stores
the tag it is currently running in a small ``installed_version.txt`` next to
the binary, so every new commit produces a new tag and triggers an update.

This is a GitHub-native replacement for PyUpdater: PyUpdater is archived and
requires a separate update server, whereas GitHub Releases is the natural host
for "auto-check GitHub for new binaries".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from version import VERSION

# --- Configuration -----------------------------------------------------
# GitHub repository that hosts the releases.
GITHUB_REPO = "tirth0jain/filepicker"
# Asset name prefix the CI uploads, e.g. "FilePicker-0.1.0-<sha>-win64.exe".
ASSET_PREFIX = "FilePicker"
# How often to check for updates (seconds). 5 minutes keeps new releases
# picked up quickly while staying well within the GitHub API unauthenticated
# rate limit (60 req/hr; 12/hr is comfortable).
CHECK_INTERVAL = 5 * 60  # every 5 minutes


def _is_frozen() -> bool:
    """True when running from a compiled (Nuitka/PyInstaller) binary."""
    return getattr(sys, "frozen", False)


def _current_exe() -> Path:
    """Path of the running executable (or main.py in dev)."""
    if _is_frozen():
        return Path(sys.executable)
    return Path(__file__).resolve().parent / "main.py"


def _installed_tag() -> str:
    """The release tag the installed binary was built from."""
    marker = _current_exe().with_name("installed_version.txt")
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return VERSION


def _split_tag(tag: str) -> tuple:
    """Split a tag like ``v0.1.0-abc123`` into ``(version_tuple, build)``."""
    tag = tag.lstrip("vV")
    if "-" in tag:
        ver, build = tag.split("-", 1)
    else:
        ver, build = tag, ""
    parts = []
    for chunk in ver.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts), build


def _latest_release_info() -> dict:
    """Fetch the latest release metadata from the GitHub API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "FilePicker-Updater"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_for_update() -> Optional[dict]:
    """Return update info if a newer binary exists, else ``None``.

    Comparison rules:
    - A higher version (e.g. 0.2.0 > 0.1.0) always updates.
    - A newer build of the same version (e.g. v0.1.0-bbb vs v0.1.0-aaa)
      updates only when the installed binary has a recorded tag that differs
      (so a fresh download of the latest build does not re-download itself).
    """
    try:
        info = _latest_release_info()
    except Exception as exc:
        print(f"[updater] could not reach GitHub: {exc}")
        return None

    latest_tag = info.get("tag_name", "")
    if not latest_tag:
        return None

    latest_ver, _ = _split_tag(latest_tag)
    current_ver, _ = _split_tag(VERSION)
    installed = _installed_tag()

    if latest_ver < current_ver:
        return None
    if latest_ver == current_ver:
        # Same version line: only update if we have a recorded installed tag
        # that differs (a newer build of the same version).
        if installed == VERSION or installed == latest_tag:
            return None

    # Find the Windows asset for this app.
    for asset in info.get("assets", []):
        name = asset.get("name", "")
        if name.startswith(ASSET_PREFIX) and name.lower().endswith(".exe"):
            return {
                "version": latest_tag,
                "url": asset["browser_download_url"],
                "name": name,
            }
    return None


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "FilePicker-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _relaunch(new_exe: Path) -> None:
    """Launch the new binary and exit the current process."""
    if _is_frozen():
        subprocess.Popen([str(new_exe)])
    else:
        subprocess.Popen([sys.executable, str(new_exe)])
    os._exit(0)


def _notice_file() -> Path:
    """File that records the last update for the post-update popup."""
    return _current_exe().with_name("last_update.txt")


def consume_update_notice() -> Optional[str]:
    """Return the ``old -> new`` update notice if one exists, then clear it."""
    notice = _notice_file()
    try:
        text = notice.read_text(encoding="utf-8").strip()
        notice.unlink()
        return text or None
    except OSError:
        return None


def download_update(update: dict) -> Optional[Path]:
    """Download the new binary to a temp location; return its path or None.

    This does NOT touch the running exe, so it is safe to do while the app is
    still processing files. The actual swap happens later in install_update().
    """
    try:
        new_file = Path(tempfile.gettempdir()) / update["name"]
        _download(update["url"], new_file)
        return new_file
    except Exception as exc:
        print(f"[updater] download failed: {exc}")
        return None


def install_update(update: dict, new_file: Path) -> bool:
    """Atomically swap the running exe with the downloaded one and relaunch.

    Only call this once the app is idle (no files being processed), because it
    replaces the running binary and exits the process. Returns True on success.
    """
    exe = _current_exe()
    old_tag = _installed_tag()
    old_file = exe.with_suffix(exe.suffix + ".old")

    try:
        # Atomic swap: rename running exe -> .old, move new -> exe, relaunch.
        if old_file.exists():
            old_file.unlink()
        exe.rename(old_file)
        new_file.rename(exe)

        # Record the installed tag so we don't re-apply the same build.
        marker = exe.with_name("installed_version.txt")
        marker.write_text(update["version"], encoding="utf-8")

        # Record the update notice so the relaunched app can show a popup
        # telling the user what version it was updated from and to.
        _notice_file().write_text(
            f"{old_tag} -> {update['version']}", encoding="utf-8"
        )

        _relaunch(exe)
        return True
    except Exception as exc:
        print(f"[updater] install failed: {exc}")
        # Try to restore the original binary.
        try:
            if not exe.exists() and old_file.exists():
                old_file.rename(exe)
        except OSError:
            pass
        return False


def apply_update(update: dict, is_busy: Optional[Callable[[], bool]] = None) -> bool:
    """Download then install, optionally waiting until the app is idle.

    When ``is_busy`` is provided, the swap is deferred (polling every 2s) until
    it returns False — i.e. until all in-progress file processing completes.
    """
    if not _is_frozen():
        print("[updater] updates only apply to compiled binaries; skipping.")
        return False

    staged = download_update(update)
    if staged is None:
        return False

    if is_busy:
        while is_busy():
            time.sleep(2)

    return install_update(update, staged)


def run_update_check() -> None:
    """Non-blocking entry point: check for updates in a background thread."""

    def worker() -> None:
        try:
            update = check_for_update()
            if update:
                print(f"[updater] new version {update['version']} available; applying…")
                apply_update(update)
        except Exception as exc:  # never let the updater crash the app
            print(f"[updater] error: {exc}")

    threading.Thread(target=worker, name="filepicker-updater", daemon=True).start()


def schedule_periodic(root) -> None:
    """Schedule periodic update checks on the Tk main loop."""
    def check() -> None:
        run_update_check()
        try:
            root.after(int(CHECK_INTERVAL * 1000), check)
        except Exception:
            pass

    root.after(int(CHECK_INTERVAL * 1000), check)