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
import zipfile
from pathlib import Path
from typing import Callable, Optional

from version import VERSION

# --- Configuration -----------------------------------------------------
# GitHub repository that hosts the releases.
GITHUB_REPO = "tirth0jain/filepicker"
# Asset name prefix the CI uploads, e.g. "FilePicker-0.1.2-<sha>-win64.zip".
ASSET_PREFIX = "FilePicker"
# How often to check for updates (seconds). 5 minutes keeps new releases
# picked up quickly while staying well within the GitHub API unauthenticated
# rate limit (60 req/hr; 12/hr is comfortable).
CHECK_INTERVAL = 5 * 60  # every 5 minutes

# Marker file the CI build drops next to the exe so the updater can tell which
# tag a running build corresponds to. Written into the release zip and then
# extracted into the app folder by install_update().
_INSTALLED_TAG_FILE = "installed_version.txt"

# Files that live next to the app but are the user's data / runtime state and
# must never be deleted or overwritten by an update.
_PRESERVE_FILES = {"config.json", "last_update.txt", _INSTALLED_TAG_FILE}


def _is_frozen() -> bool:
    """True when running from a compiled (Nuitka) binary."""
    return getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False))


def _current_exe() -> Path:
    """Path of the running executable (or main.py in dev)."""
    if _is_frozen():
        return Path(sys.executable)
    return Path(__file__).resolve().parent / "main.py"


def _installed_tag() -> str:
    """The release tag the installed binary was built from.

    Prefers the recorded tag next to the exe, falling back to VERSION.
    """
    marker = _current_exe().with_name(_INSTALLED_TAG_FILE)
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


def check_for_update(strict: bool = True) -> Optional[dict]:
    """Return update info if a newer binary exists, else ``None``.

    ``strict=True`` (used by the automatic periodic check): only a higher
    version (e.g. 0.2.6 > 0.2.5) is treated as an update. Builds of the *same*
    version line — even with a different GitHub Actions id in the tag — are
    ignored, so the app never nags when it is already on the latest release.

    ``strict=False`` (used by the manual tray "Check for updates"): also
    updates on a newer build of the same version (e.g. v0.2.5-bbb vs
    v0.2.5-aaa) when the recorded installed tag differs, so the user can pull
    the newest build on demand.
    """
    try:
        info = _latest_release_info()
    except Exception as exc:
        print(f"[updater] could not reach GitHub: {exc}")
        return None

    latest_tag = info.get("tag_name", "")
    if not latest_tag:
        return None

    latest_ver, latest_build = _split_tag(latest_tag)
    current_ver, _ = _split_tag(VERSION)

    if latest_ver < current_ver:
        return None
    if latest_ver == current_ver:
        if strict:
            # Auto check: same version line == up to date. Ignore the Actions
            # id so a new build of the installed version does not re-prompt.
            return None
        # Manual check: a newer build of the same version updates only when the
        # recorded installed tag differs (don't re-download the exact build).
        installed = _installed_tag().strip().lower().lstrip("v")
        latest_norm = latest_tag.strip().lower().lstrip("v")
        if installed == latest_norm or installed == current_ver:
            return None

    # Find the Windows zip asset for this app.
    for asset in info.get("assets", []):
        name = asset.get("name", "")
        if name.startswith(ASSET_PREFIX) and name.lower().endswith(".zip"):
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
    """Download the new build zip to a temp location; return its path or None.

    This does NOT touch the running app, so it is safe to do while files are
    still being processed. The actual swap happens later in install_update().
    """
    try:
        new_file = Path(tempfile.gettempdir()) / update["name"]
        _download(update["url"], new_file)
        return new_file
    except Exception as exc:
        print(f"[updater] download failed: {exc}")
        return None


def _replace_file(src: Path, dst: Path) -> None:
    """Copy one file; if the destination is locked (in use), rename it aside
    first (Windows lets you rename a file that's memory-mapped by a running
    process, but not overwrite it), then copy the new file in."""
    try:
        shutil.copy2(src, dst)
        return
    except PermissionError:
        pass
    old = dst.with_name(dst.name + ".old")
    try:
        dst.rename(old)
    except PermissionError:
        old.unlink(missing_ok=True)
        dst.rename(old)
    shutil.copy2(src, dst)


def install_update(update: dict, staged_zip: Path) -> bool:
    """Replace the running standalone app folder with the new build and relaunch.

    Only call this once the app is idle (no files being processed), because it
    replaces the running binary and exits the process. Returns True on success.

    The swap is "mirror to the new build": after it completes, the app folder
    contains exactly the new build's files (plus preserved user files such as
    config.json). Old files that are no longer in the new build are removed,
    locked ones are renamed to ``.old`` (cleaned up on next launch).
    """
    exe = _current_exe()          # e.g. .../FilePicker/FilePicker.exe
    app_dir = exe.parent
    old_tag = _installed_tag()

    # 1. Extract the new build to a temp folder.
    extract_root = Path(tempfile.gettempdir()) / f"FilePicker-new-{update['version'].lstrip('vV')}"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    try:
        with zipfile.ZipFile(staged_zip) as zf:
            zf.extractall(extract_root)
    except Exception as exc:
        print(f"[updater] could not extract update: {exc}")
        return False

    new_dir = extract_root
    new_exe = new_dir / "FilePicker.exe"
    if not new_exe.exists():
        # Some releases may carry a "FilePicker.dist" wrapper; unwrap it.
        nested = sorted(new_dir.glob("**/FilePicker.exe"))
        if nested:
            new_exe = nested[0]
            new_dir = new_exe.parent
        else:
            print("[updater] new build has no FilePicker.exe; aborting update")
            return False

    # Read the tag marker shipped inside the new build (if present).
    new_tag_file = new_dir / _INSTALLED_TAG_FILE
    new_tag = update["version"]
    if new_tag_file.exists():
        try:
            new_tag = new_tag_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # 2. Rename the running exe out of the way (Windows allows renaming the
    #    running image, but not deleting it).
    old_exe = app_dir / "FilePicker.exe.old"
    if old_exe.exists():
        old_exe.unlink(missing_ok=True)
    try:
        exe.rename(old_exe)
    except OSError as exc:
        print(f"[updater] could not rename running exe: {exc}")
        return False

    # 3. Copy the new build's files over the app folder, handling any locked
    #    DLLs by renaming them aside first. User data (config.json, markers)
    #    is never overwritten by an update.
    new_files = {f.relative_to(new_dir) for f in new_dir.rglob("*") if f.is_file()}
    try:
        for src in sorted(new_dir.rglob("*")):
            if src.is_file():
                rel = src.relative_to(new_dir)
                if rel.name in _PRESERVE_FILES:
                    continue
                dst = app_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(src, dst)
    except Exception as exc:
        print(f"[updater] failed copying new build: {exc}")
        return False

    # 4. Remove files that exist in the app folder but are not part of the new
    #    build (and are not user data), so the folder mirrors the new build.
    #    Locked files can't be deleted while running, and the freshly renamed
    #    .old files must survive until the next launch's cleanup, so both are
    #    skipped here.
    for existing in list(app_dir.iterdir()):
        name = existing.name
        if name in _PRESERVE_FILES or name.endswith(".old"):
            continue
        rel = Path(name)
        if rel in new_files:
            continue
        try:
            if existing.is_file():
                existing.unlink(missing_ok=True)
            else:
                shutil.rmtree(existing, ignore_errors=True)
        except (PermissionError, OSError):
            try:
                existing.rename(existing.with_name(existing.name + ".old"))
            except OSError:
                pass

    # 5. Record installed tag + update notice.
    (app_dir / _INSTALLED_TAG_FILE).write_text(new_tag, encoding="utf-8")
    _notice_file().write_text(f"{old_tag} -> {new_tag}", encoding="utf-8")

    # 6. Clean up the temp extraction (leftover .old files are removed on next
    #    launch by main.py's startup cleanup).
    shutil.rmtree(extract_root, ignore_errors=True)
    staged_zip.unlink(missing_ok=True)

    # 7. Relaunch the (now-new) exe and exit this process.
    _relaunch(exe)
    return True


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