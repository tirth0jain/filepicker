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

# Runtime marker the updater writes BEFORE swapping files, so the freshly
# launched process knows there is a swap to finish (re-copy files that were
# locked, remove every `*.old`, drop the staging folder). It is written next
# to the app and removed by resume_pending_update().
_PENDING_UPDATE_FILE = "update_state.json"

# Files that live next to the app but are the user's data / runtime state and
# must never be deleted or overwritten by an update.
# NOTE: the API token files are never shipped inside the build, so without
# this list the stale-file prune would delete them during an update (and the
# next launch's deep `.old` cleanup would finish the job). Preserving them
# keeps the live GitHub config push and OCR working after an update.
_PRESERVE_FILES = {
    "config.json",
    "last_update.txt",
    _INSTALLED_TAG_FILE,
    "github_token.txt",
    "opencode_token.txt",
}


def _is_frozen() -> bool:
    """True when running from a compiled (Nuitka) binary."""
    # Nuitka sets ``__compiled__`` in each compiled module and may set
    # ``sys.frozen`` / ``sys.nuitka_standalone`` depending on the plugin
    # version.  As a last resort, if the running executable is literally
    # ``FilePicker.exe`` we are frozen — this handles the exact failure the
    # user saw (``main.py -> FilePicker.exe.old`` on a frozen install).
    if getattr(sys, "frozen", False):
        return True
    if bool(getattr(sys, "nuitka_standalone", False)):
        return True
    if globals().get("__compiled__"):
        return True
    try:
        exe_name = Path(sys.executable).name.lower()
        if exe_name == "filepicker.exe":
            return True
        # Any non-python exe is treated as frozen (covers renamed installs)
        if exe_name not in ("python.exe", "pythonw.exe", "python"):
            # If we're not running under a stock interpreter, assume frozen
            # when the exe lives next to a FilePicker.exe sibling.
            cand = Path(sys.executable).with_name("FilePicker.exe")
            if cand.exists():
                return True
    except Exception:
        pass
    return False


def _current_exe() -> Path:
    """Path of the running executable (or main.py in dev).

    Prefers a ``FilePicker.exe`` next to the interpreter/this file — this is
    the correct exe even when frozen detection fails (the bug the user hit:
    ``main.py -> FilePicker.exe.old`` on a frozen install).
    """
    # 1. FilePicker.exe next to the running image — most reliable for frozen.
    try:
        cand = Path(sys.executable).with_name("FilePicker.exe")
        if cand.exists():
            return cand
    except Exception:
        pass
    try:
        cand2 = Path(__file__).resolve().parent / "FilePicker.exe"
        if cand2.exists():
            return cand2
    except Exception:
        pass
    if _is_frozen():
        return Path(sys.executable)
    main_py = Path(__file__).resolve().parent / "main.py"
    if main_py.exists():
        return main_py
    return Path(sys.executable)


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
        if installed == latest_norm:
            return None
        # build.py ships the marker as bare "v0.6.3" (no build id); a fresh
        # install of the latest version is already up to date — don't offer a
        # redundant same-version re-download until an auto-update has recorded
        # the real tag (which includes the build id).
        inst_ver, inst_build = _split_tag(installed)
        if not inst_build and inst_ver == current_ver:
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


def _relaunch(new_exe: Path) -> bool:
    """Launch the new binary. Returns True once the process has started.

    The caller only exits the old process when this returns True — if the new
    exe cannot be started the old process stays alive and the pending swap is
    finished by the next launch instead (see resume_pending_update).
    """
    if not new_exe.exists():
        print(f"[updater] relaunch: new exe missing at {new_exe}")
        return False
    try:
        # Small delay to let the OS release handles after the copy
        time.sleep(0.5)
    except Exception:
        pass
    cwd = str(new_exe.parent) if new_exe.parent.exists() else None
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    kwargs: dict = {"close_fds": True}
    if cwd:
        kwargs["cwd"] = cwd
    if flags:
        kwargs["creationflags"] = flags
    cmd = [str(new_exe)] if _is_frozen() else [sys.executable, str(new_exe)]

    for attempt in range(1, 4):
        try:
            subprocess.Popen(cmd, **kwargs)
            print(f"[updater] relaunched {new_exe} (attempt {attempt})")
            return True
        except Exception as exc:
            print(f"[updater] relaunch attempt {attempt} failed: {exc}")
            try:
                time.sleep(1)
            except Exception:
                pass
    # Final bare fallback without cwd/flags
    try:
        subprocess.Popen(cmd)
        print("[updater] relaunch bare fallback succeeded")
        return True
    except Exception as exc2:
        print(f"[updater] relaunch failed: {exc2}")
        return False


def _cleanup_old_files_deep(
    app_dir: Path,
    retries: int = 3,
    retry_delay: float = 0.5,
) -> None:
    """Remove every `*.old` file/dir recursively (leftovers from a swap).

    The old `_cleanup_old_files` only checked the top level; Nuitka leaves
    `.old` files deep inside (e.g. `lib/...`), so we must walk recursively.
    Deletions are retried a few times: right after a relaunch the old image
    files can still be briefly locked (dying process, Defender scan,
    Explorer), and a single attempt would silently leave them behind.
    """
    def _remove(path: Path) -> bool:
        for _ in range(retries):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                if not path.exists():
                    return True
            except OSError:
                pass
            time.sleep(retry_delay)
        return False

    try:
        for old in list(app_dir.rglob("*.old")):
            _remove(old)
    except Exception:
        pass


def resume_pending_update(app_dir: Path) -> Optional[str]:
    """Finish an interrupted in-place update from the freshly started process.

    install_update() swaps the running exe and copies the new build while the
    old process is still alive, so some files stay locked and are renamed to
    ``.old`` (which a live process can never delete). The NEW process — started
    right after the swap — calls this at startup to:

    1. re-copy any files that failed while the old process was alive,
    2. remove every ``*.old`` leftover (safe now: the old process has exited),
    3. prune stale files that are no longer part of the new build,
    4. drop the staging folder and the ``update_state.json`` marker,
    5. record the ``old -> new`` notice (returned so the caller can show it).

    Idempotent and never raises: with no marker it is a no-op, so every normal
    launch is unaffected.
    """
    marker = app_dir / _PENDING_UPDATE_FILE
    if not marker.exists():
        return None

    state: dict = {}
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        pass
    staging = Path(str(state.get("staging") or ""))
    new_tag = str(state.get("new_tag") or "")
    old_tag = str(state.get("old_tag") or "")

    # Give the previous process a moment to fully release its image files.
    try:
        time.sleep(0.5)
    except Exception:
        pass

    try:
        if staging.is_dir() and (staging / "FilePicker.exe").exists():
            new_files = {f.relative_to(staging) for f in staging.rglob("*") if f.is_file()}
            for src in sorted(staging.rglob("*")):
                if not src.is_file():
                    continue
                rel = src.relative_to(staging)
                if rel.name in _PRESERVE_FILES:
                    continue
                dst = app_dir / rel
                try:
                    # Only top up files that are missing or differ in size —
                    # the old process usually copied everything already.
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                        shutil.copy2(src, dst)
                except OSError:
                    pass
            _prune_stale_files(app_dir, new_files)
    except Exception as exc:
        print(f"[updater] resume copy warning: {exc}")

    # Remove every *.old leftover — safe now that the old process has exited.
    _cleanup_old_files_deep(app_dir)

    # Drop the extraction folder and the marker.
    try:
        shutil.rmtree(staging, ignore_errors=True)
    except Exception:
        pass
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass

    if new_tag and old_tag:
        notice = f"{old_tag} -> {new_tag}"
        try:
            (app_dir / "last_update.txt").write_text(notice, encoding="utf-8")
        except OSError:
            pass
        print(f"[updater] resumed update: {notice}")
        return notice
    return None


def _write_batch_and_launch(
    app_dir: Path,
    new_dir: Path,
    extract_root: Path,
    new_tag: str,
    old_tag: str,
) -> None:
    """Write a Windows batch file that swaps the app after this process exits.

    The running .exe and its DLLs are locked while the process is alive, so
    an in-place `shutil.copy2` often leaves `.old` files and a half-updated
    install (the bug the user saw). A batch that runs *after* `os._exit`
    can copy without locks, clean `*.old` recursively, repair the Startup
    shortcut and relaunch — no SmartScreen (no Zone.Identifier) and no
    manual double-click needed.
    """
    try:
        batch_path = Path(tempfile.gettempdir()) / f"FilePicker_update_{os.getpid()}.bat"
        # Remove preserved files from the new build so the batch never
        # overwrites the user's data — but only if the user already has them.
        # On a fresh install (no config.json yet) we *do* want to copy the
        # shipped config.
        for name in _PRESERVE_FILES:
            try:
                if (app_dir / name).exists():
                    (new_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

        # Shortcut repair — must match startup.py's _target for frozen
        ps_shortcut = (
            f"$ws=New-Object -ComObject WScript.Shell;"
            f"$s=$ws.CreateShortcut(\"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\FilePicker.lnk\");"
            f"$s.TargetPath='{app_dir}\\FilePicker.exe';"
            f"$s.WorkingDirectory='{app_dir}';$s.Save()"
        )

        batch = f"""@echo off
setlocal
set "APP_DIR={app_dir}"
set "NEW_DIR={new_dir}"
set "EXTRACT_ROOT={extract_root}"
set "EXE=FilePicker.exe"
:: Wait for the old FilePicker.exe to fully exit (handles released)
timeout /t 2 /nobreak >nul
:waitloop
tasklist /FI "IMAGENAME eq %EXE%" 2>nul | find /I "%EXE%" >nul
if %ERRORLEVEL%==0 (
  timeout /t 1 /nobreak >nul
  goto waitloop
)
:: Copy new build over app dir (xcopy is native, no PowerShell window)
xcopy "%NEW_DIR%\\*" "%APP_DIR%\\" /E /Y /H /Q >nul 2>&1
:: Fallback PowerShell copy for long paths (hidden)
powershell -NoProfile -WindowStyle Hidden -Command "Copy-Item -Path '%NEW_DIR%\\*' -Destination '%APP_DIR%' -Recurse -Force -ErrorAction SilentlyContinue" >nul 2>&1
:: Clean any leftover .old recursively (old in-place swaps)
del /S /Q "%APP_DIR%\\*.old" >nul 2>&1
for /D %%i in ("%APP_DIR%\\*.old") do rd /S /Q "%%i" >nul 2>&1
powershell -NoProfile -WindowStyle Hidden -Command "Get-ChildItem -Path '%APP_DIR%' -Recurse -Filter '*.old' | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue" >nul 2>&1
:: Record new version + update notice
echo {new_tag}> "%APP_DIR%\\{_INSTALLED_TAG_FILE}"
echo {old_tag} -> {new_tag}> "%APP_DIR%\\last_update.txt"
:: Clean up the temp extraction folder
rd /S /Q "%EXTRACT_ROOT%" >nul 2>&1
:: Repair Startup shortcut (hidden)
powershell -NoProfile -WindowStyle Hidden -Command "{ps_shortcut}" >nul 2>&1
:: Relaunch (no SmartScreen — file was copied locally, not downloaded)
start "" "%APP_DIR%\\%EXE%"
:: Self-delete
del "%~f0" >nul 2>&1
"""
        batch_path.write_text(batch, encoding="utf-8")
        # Launch detached so it survives os._exit
        try:
            # CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags |= subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            subprocess.Popen(
                ["cmd.exe", "/c", str(batch_path)],
                creationflags=flags,
                close_fds=True,
                cwd=str(tempfile.gettempdir()),
            )
        except Exception:
            # Fallback without flags (e.g. dev on Linux where cmd doesn't exist — just try)
            try:
                subprocess.Popen([str(batch_path)], shell=True, close_fds=True)
            except Exception as exc2:
                print(f"[updater] batch launch failed: {exc2}")
                raise
        print(f"[updater] launched batch updater {batch_path}")
    except Exception as exc:
        print(f"[updater] batch write failed: {exc}")
        raise


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


class UpdateError(Exception):
    """Raised when an update cannot be applied; carries a user-friendly reason."""


def _replace_file(src: Path, dst: Path) -> bool:
    """Copy one file; never raises. Returns True on success.

    If the destination is locked (in use by the running process), Windows lets
    you RENAME it aside (``.old``) but not overwrite it, so we rename and then
    copy the new file in. If even that fails, we return False and leave the
    file for resume_pending_update(), which retries from the freshly started
    process once the old process has exited and released its locks.
    """
    try:
        shutil.copy2(src, dst)
        return True
    except PermissionError:
        pass
    old = dst.with_name(dst.name + ".old")
    try:
        old.unlink(missing_ok=True)
        dst.rename(old)
    except OSError:
        return False
    try:
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def _prune_stale_files(app_dir: Path, new_files: set) -> None:
    """Remove files/dirs in *app_dir* that are not part of the new build.

    Locked leftovers are renamed to ``.old`` (removed by the next-launch deep
    clean). Never deletes user data (``_PRESERVE_FILES``) or the update marker.
    Never raises.
    """
    try:
        for existing in sorted(app_dir.rglob("*"), reverse=True):  # files before dirs
            try:
                rel = existing.relative_to(app_dir)
            except ValueError:
                continue
            if rel.name in _PRESERVE_FILES or rel.name == _PENDING_UPDATE_FILE:
                continue
            if ".old" in str(rel):
                continue
            if rel in new_files:
                continue
            try:
                if existing.is_dir():
                    # Keep the dir if it contains any new file.
                    keep = any(
                        str(nf).startswith(str(rel) + os.sep) or str(nf) == str(rel)
                        for nf in new_files
                    )
                    if keep:
                        continue
                    if not any(existing.iterdir()):
                        existing.rmdir()
                    else:
                        shutil.rmtree(existing, ignore_errors=True)
                else:
                    existing.unlink(missing_ok=True)
            except (PermissionError, OSError):
                try:
                    existing.rename(existing.with_name(existing.name + ".old"))
                except OSError:
                    pass
    except Exception as exc:
        print(f"[updater] stale cleanup warning: {exc}")


def install_update(update: dict, staged_zip: Path) -> bool:
    """Replace the running standalone app folder with the new build and relaunch.

    Only call this once the app is idle (no files being processed), because it
    replaces the running binary and exits the process. Returns True on success,
    or raises :class:`UpdateError` with a descriptive reason on failure (the
    caller surfaces it to the user instead of silently doing nothing).

    Swap strategy (Defender-safe; the self-deleting batch updater is disabled):

    * The running exe is renamed to ``FilePicker.exe.old`` FIRST and the new
      exe is copied in — its name is free after the rename, so the app is
      always relaunchable and the swap never rolls back mid-way.
    * All other files are copied best-effort (``_replace_file`` never raises);
      files locked by the still-running process are renamed to ``.old``.
    * A ``update_state.json`` marker is written before any file is touched, so
      the freshly launched process knows there is a swap to finish. It then
      runs :func:`resume_pending_update`: re-copies anything that was locked,
      deep-cleans every ``*.old`` (the old process is dead by then), prunes
      stale files and drops the staging folder.
    """
    exe = _current_exe()          # e.g. .../FilePicker/FilePicker.exe
    app_dir = exe.parent
    old_tag = _installed_tag()

    def fail(reason: str) -> None:
        raise UpdateError(reason)

    def abort_pending() -> None:
        # Remove the marker + staging so a later launch does not try to resume
        # a swap that never started.
        try:
            (app_dir / _PENDING_UPDATE_FILE).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(extract_root, ignore_errors=True)
        except OSError:
            pass
        try:
            staged_zip.unlink(missing_ok=True)
        except OSError:
            pass

    # 0. Clear leftovers from previous attempts (files mapped by THIS process,
    # if any, are finished by resume_pending_update() after relaunch).
    _cleanup_old_files_deep(app_dir)

    # 1. Extract the new build to a temp folder.
    extract_root = Path(tempfile.gettempdir()) / f"FilePicker-new-{update['version'].lstrip('vV')}"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    try:
        with zipfile.ZipFile(staged_zip) as zf:
            zf.extractall(extract_root)
    except Exception as exc:
        abort_pending()
        fail(f"Could not extract the downloaded update: {exc}")

    new_dir = extract_root
    new_exe = new_dir / "FilePicker.exe"
    if not new_exe.exists():
        # Some releases may carry a "FilePicker.dist" wrapper; unwrap it.
        nested = sorted(new_dir.glob("**/FilePicker.exe"))
        if nested:
            new_exe = nested[0]
            new_dir = new_exe.parent
        else:
            abort_pending()
            fail("The downloaded update has no FilePicker.exe inside it.")

    # Read the tag marker shipped inside the new build (if present).
    new_tag_file = new_dir / _INSTALLED_TAG_FILE
    new_tag = update["version"]
    if new_tag_file.exists():
        try:
            new_tag = new_tag_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # 1b. Pending-update marker — written BEFORE touching the app so the new
    # process can finish the swap even if we crash / exit mid-way.
    try:
        (app_dir / _PENDING_UPDATE_FILE).write_text(
            json.dumps({
                "staging": str(extract_root),
                "new_tag": new_tag,
                "old_tag": old_tag,
            }),
            encoding="utf-8",
        )
    except OSError as exc:
        abort_pending()
        fail(f"Could not write the update marker: {exc}")

    # Prefer the project config.json shipped in the new build, unless the
    # installed app already has one (it is never overwritten).
    shipped_config = new_dir / "config.json"
    installed_config = app_dir / "config.json"
    if shipped_config.exists() and not installed_config.exists():
        try:
            shutil.copy2(shipped_config, installed_config)
        except OSError:
            pass

    # 2. Swap the running exe FIRST: rename it aside, then copy the new exe
    # in. Its name is free after the rename, so this cannot realistically fail
    # and the app is always relaunchable.
    old_exe = app_dir / "FilePicker.exe.old"
    if old_exe.exists():
        try:
            old_exe.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        exe.rename(old_exe)
    except OSError as exc:
        abort_pending()
        fail(f"Could not rename the running FilePicker.exe "
             f"(it may be locked): {exc}")
    try:
        shutil.copy2(new_exe, exe)
    except OSError as exc:
        # Nothing else has been touched yet — restore the old exe and abort.
        try:
            old_exe.rename(exe)
        except OSError:
            pass
        abort_pending()
        fail(f"Could not copy the new FilePicker.exe into place: {exc}")

    # 3. Mirror the rest of the new build. Never raises: anything still locked
    # (DLLs/.pyd mapped by the running process) is renamed to `.old` and the
    # new file is copied over; failures are finished by resume_pending_update()
    # in the freshly started process.
    new_files = {f.relative_to(new_dir) for f in new_dir.rglob("*") if f.is_file()}
    for src in sorted(new_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(new_dir)
        if rel.name in _PRESERVE_FILES:
            continue
        dst = app_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        _replace_file(src, dst)

    # 4. Remove stale files so app_dir mirrors the new build; deep-clean any
    # `.old` (locked ones are left for resume, which runs once we're gone).
    _prune_stale_files(app_dir, new_files)
    _cleanup_old_files_deep(app_dir)

    # 5. Record installed tag + update notice.
    try:
        (app_dir / _INSTALLED_TAG_FILE).write_text(new_tag, encoding="utf-8")
        _notice_file().write_text(f"{old_tag} -> {new_tag}", encoding="utf-8")
    except OSError as exc:
        print(f"[updater] marker write failed: {exc}")

    # 6. Not needed to clean the zip anymore; resume removes the staging dir.
    try:
        staged_zip.unlink(missing_ok=True)
    except OSError:
        pass

    # 7. Relaunch. Only exit once the new process has actually started; the
    # staging folder + marker stay behind for it to finish the swap. If the
    # relaunch fails we remain running (the swap is completed on next launch).
    print(f"[updater] update {new_tag} applied; relaunching…")
    if _relaunch(exe):
        print(f"[updater] exiting old process {os.getpid()} for update")
        os._exit(0)
    print("[updater] relaunch failed; the pending swap will be finished on next launch")
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