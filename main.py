"""FilePicker — Windows background download organiser.

Entry point that ties together the config, folder watcher, popup dialog and
file router. Run with::

    python main.py

The utility stays resident in the background (hidden main window), watches the
configured download folder, and pops up a metadata dialog whenever a download
completes.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

# Ensure the app's own folder is importable no matter the working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import customtkinter as ctk

from config import ConfigManager
from ocr import MAX_CONCURRENT_OCR as _OCR_MAX
from organizer import OrganizeRequest, organize, output_paths
from popup import FilePickerPopup, ask_duplicate_action
from version import VERSION
from watcher import DownloadWatcher

# How long the FIRST file's read may hold the rest of the batch back. The
# first popup's file is read on its own (it gets the whole gateway, so the
# popup the user is looking at fills in as fast as possible) and the other
# downloads are sent together as soon as that read finishes. This cap is only
# a safety valve: if the first read is genuinely slow (a retry storm, a huge
# scan) the batch is not held hostage — after this many seconds the remaining
# files are sent anyway.
_OCR_FIRST_HEAD_START = 20.0

# Auto-start registration attempts at launch (see _ensure_startup): at login
# the user profile and the registry can still be settling, and a transient
# failure there would silently mean "never starts with Windows" until the next
# manual launch.
_STARTUP_ATTEMPTS = 3
_STARTUP_RETRY_DELAY = 5.0

# How often the UI thread checks the popup queue. 50ms (it was 100ms) so a
# file the watcher just declared complete opens its popup as soon as the
# queue entry exists instead of up to a tenth of a second later — part of
# making the popup feel immediate after a scan lands.
_POPUP_POLL_MS = 50

# How long to wait for the watch folder (a mapped network drive) to appear
# before giving up: 180 x 10s = 30 minutes, which comfortably covers a drive
# that reconnects a while after login (or after a VPN comes up).
_WATCH_DIR_ATTEMPTS = 180
_WATCH_DIR_RETRY_DELAY = 10.0


def _setup_file_logging() -> None:
    """Mirror all prints to FilePicker.log next to the exe (visible even with --windows-console-mode=disable)."""
    try:
        if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)) or Path(sys.executable).name.lower() == "filepicker.exe":
            log_path = Path(sys.executable).parent / "FilePicker.log"
        else:
            log_path = Path(__file__).resolve().parent / "FilePicker.log"
        import logging

        logging.basicConfig(
            filename=str(log_path),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            filemode="a",
        )

        class _Writer:
            def __init__(self, level):
                self.level = level

            def write(self, msg):
                if msg and msg.strip():
                    logging.log(self.level, msg.strip())

            def flush(self):
                pass

        sys.stdout = _Writer(logging.INFO)  # type: ignore
        sys.stderr = _Writer(logging.ERROR)  # type: ignore
        print(f"[filepicker] logging to {log_path} v{VERSION} frozen={getattr(sys,'frozen',False)} pid={os.getpid()}")
    except Exception as exc:
        try:
            print(f"[filepicker] log setup failed: {exc}")
        except Exception:
            pass


# Popup palette (matches popup.py / viewer.py).
_BG = "#15151d"
_BG_SECONDARY = "#1f1f2b"
_BG_FIELD = "#262633"
_ACCENT = "#5b8cff"
_ACCENT_HOVER = "#3f6fe0"
_TEXT = "#f2f2f7"
_TEXT_MUTED = "#b6b6c9"
_DANGER = "#ff6b6b"

# How long the "updating" notice is shown before the update is applied
# automatically (the app then swaps the exe and relaunches on its own).
_UPDATE_APPLY_DELAY_MS = 3000

# Wait this long before re-trying an update whose INSTALL failed (30 min, 1 h,
# 2 h, then 6 h for every further attempt). The periodic check runs every
# 5 minutes and re-downloads the whole asset each time, so a release that
# cannot be installed on this machine (a locked FilePicker.exe.old, a blocked
# relaunch) must back off instead of pulling ~55 MB five times an hour.
_UPDATE_RETRY_BACKOFF = (1800, 3600, 7200, 21600)


def show_update_notice(root, notice: str) -> None:
    """Show a small popup telling the user the app was updated."""
    win = ctk.CTkToplevel(root)
    win.title(f"FilePicker v{VERSION} — Updated")
    win.geometry("440x200")
    win.configure(fg_color=_BG)
    win.transient(root)
    # Intentionally NOT modal (no grab_set): a modal grab here would conflict
    # with the download popup's grab if a file completes while this is open.
    win.attributes("-topmost", True)

    ctk.CTkLabel(
        win, text="✅ FilePicker has been updated",
        font=ctk.CTkFont(size=16, weight="bold"), text_color=_TEXT,
    ).pack(pady=(26, 8))
    ctk.CTkLabel(
        win, text=notice, font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED,
    ).pack(pady=(0, 6))
    ctk.CTkLabel(
        win, text=f"Now running v{VERSION}",
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
    ).pack(pady=(0, 14))
    ctk.CTkButton(
        win, text="OK", width=120, command=win.destroy,
        fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
    ).pack(pady=(0, 18))
    win.lift()
    win.focus_force()


class FilePickerController:
    """Owns the hidden root window, the watcher and the popup flow."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._popup_queue: "queue.Queue[Path]" = queue.Queue()
        self._popup_active = False
        self._organize_active = False
        self._pending_update = None  # (update_dict, staged_path) awaiting install
        self._update_dialog_open = False  # true while the "updating" dialog is up
        # Failed install attempts per release tag -> (attempts, next try at
        # monotonic). Without this a build that cannot be installed (a locked
        # FilePicker.exe.old, a blocked relaunch) made the app re-download the
        # whole ~55 MB asset every 5 minutes, forever.
        self._update_retries: Dict[str, tuple] = {}
        self._ui_commands: "queue.Queue[str]" = queue.Queue()  # tray -> main thread
        self._tray = None
        self._root = None
        self._current_popup = None  # the popup currently on screen (so live config can refresh it)
        self._ocr_pool = None  # OcrPool — reads every download at once (see _OCR_MAX)
        self._watcher = None     # DownloadWatcher, started once the folder exists
        self._file_order: list = []  # completed files in arrival order
        self._popups_shown = 0       # popups displayed so far (1-based next)
        self._ocr_submitted = 0      # how many of _file_order were OCR-submitted
        self._ocr_lock = threading.Lock()  # guards _ocr_submitted (watcher + UI threads)
        self._ocr_released = False   # True once the rest of the batch may be sent
        self._ocr_head_timer = None  # safety valve for a slow first read

    # ------------------------------------------------------------------
    def _build_root(self) -> None:
        self._root = ctk.CTk()
        self._root.withdraw()  # hidden background window
        self._root.title(f"FilePicker v{VERSION}")
        self._root.protocol("WM_DELETE_WINDOW", self._root.destroy)

    def _cleanup_old_files(self) -> None:
        """Remove leftover '.old' files from a previous update swap.

        During an update the running exe/DLLs are renamed to '.old' so the new
        build can take their place; those leftovers can't be deleted while the
        old process is alive, so they're cleaned up on the next launch.
        Uses recursive scan because the batch updater and old in-place code
        leave `.old` files deep inside subfolders (e.g. `PIL/*.old`).
        """
        try:
            if getattr(sys, "frozen", False):
                app_dir = Path(sys.executable).parent
            else:
                app_dir = Path(__file__).resolve().parent
            # Try updater's deep cleaner first (handles rglob)
            try:
                from updater import _cleanup_old_files_deep
                _cleanup_old_files_deep(app_dir)
                return
            except ImportError:
                pass
            for old in app_dir.rglob("*.old"):
                try:
                    if old.is_dir():
                        shutil.rmtree(old, ignore_errors=True)
                    else:
                        old.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass

    def _schedule_old_cleanup_retries(self, minutes: float = 2.0) -> None:
        """Keep retrying the `.old` cleanup shortly after startup.

        Right after a relaunch, leftover `*.old` files can still be briefly
        locked (the dying process's image, a Defender scan, Explorer), so the
        single startup pass can miss them. Retry every 15s for the first few
        minutes until no `.old` files remain.
        """
        try:
            from updater import _cleanup_old_files_deep
        except ImportError:
            return

        def retry() -> None:
            if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)):
                app_dir = Path(sys.executable).resolve().parent
            else:
                app_dir = Path(__file__).resolve().parent
            deadline = time.monotonic() + minutes * 60
            while time.monotonic() < deadline:
                try:
                    _cleanup_old_files_deep(app_dir)
                    if not list(app_dir.rglob("*.old")):
                        return
                except Exception:
                    pass
                time.sleep(15)

        threading.Thread(target=retry, name="filepicker-old-cleanup", daemon=True).start()

    def _show_update_notice_if_any(self) -> None:
        """Show a popup if the app was just updated (old -> new)."""
        try:
            from updater import consume_update_notice
            notice = consume_update_notice()
            if notice:
                self._root.after(400, lambda: show_update_notice(self._root, notice))
        except Exception as exc:
            print(f"[filepicker] update notice error: {exc}")

    def _on_file_completed(self, path: Path) -> None:
        """Called from the watcher's worker thread when a file settles."""
        self._popup_queue.put(path)
        self._file_order.append(Path(path))
        # OCR starts the moment the file lands: the FIRST file — the one whose
        # popup opens first — is read on its own so the popup the user is
        # looking at fills in as fast as possible, and the rest of the batch is
        # sent together the instant that read finishes (see _submit_ocr_all).
        try:
            self._submit_ocr_all()
        except Exception as exc:
            print(f"[filepicker] background OCR submit error: {exc}")

    def _poll_popups(self) -> None:
        """Main-thread polling loop that shows one popup at a time."""
        # Drain tray/background commands on the main thread.
        while True:
            try:
                cmd = self._ui_commands.get_nowait()
            except queue.Empty:
                break
            if cmd == "check_update":
                self._check_update_now()
            elif cmd == "force_sync":
                self._force_sync_now()
            elif cmd == "push_config":
                self._push_config_now()
            elif cmd == "toggle_startup":
                self._toggle_auto_start()
            elif cmd == "quit":
                self._root.destroy()

        if not self._popup_active:
            try:
                path = self._popup_queue.get_nowait()
            except queue.Empty:
                path = None
            if path is not None:
                self._popup_active = True
                try:
                    self._show_popup(path)
                except Exception as exc:  # never wedge the popup loop
                    print(f"[filepicker] popup error: {exc}")
                finally:
                    self._popup_active = False
        self._maybe_install_update()
        self._root.after(_POPUP_POLL_MS, self._poll_popups)

    def _show_popup(self, path: Path) -> None:
        self._popups_shown += 1
        # Safety net: if this file arrived before the OCR pool existed (pool
        # created at startup, or OCR enabled later), make sure it is queued.
        # A file still held back by the first-read sequencing is submitted by
        # the popup itself (_start_ocr), so the file on screen never waits.
        self._submit_ocr_all()
        # Log both ends of a popup's life: with the popup invisible (or never
        # built) the queue is blocked, and the log then says which of the two
        # happened instead of leaving a silent gap next to the OCR lines (see
        # popup._ensure_popup_visible for the visibility watchdog).
        waiting = self._popup_queue.qsize()
        print(f"[filepicker] popup {self._popups_shown} for {path.name} "
              f"({waiting} more waiting)")
        popup = FilePickerPopup(
            config=self.config,
            file_path=path,
            on_submit=self._handle_submit,
            on_skip=self._handle_skip,
            on_skip_all=lambda p=path: self._handle_skip_all(p),
            ocr_pool=self._ocr_pool,
        )
        self._current_popup = popup
        try:
            # Blocking until the modal is dismissed.
            popup.show()
        finally:
            self._current_popup = None
            print(f"[filepicker] popup {self._popups_shown} closed ({path.name})")

    def _submit_ocr_all(self) -> None:
        """Queue OCR for the files that have landed, first popup first.

        Order matters: the FIRST file — the one whose popup opens first — is
        read ON ITS OWN, so the popup the user is actually looking at gets the
        whole gateway and fills in as fast as possible. The rest of the batch
        is then sent TOGETHER the moment that first read finishes (or after
        :data:`_OCR_FIRST_HEAD_START` seconds if it is slow), so they are
        still all read simultaneously while the user works through the queue.
        Files that arrive after the batch was released are read immediately.

        Called from two places:
        - every file arrival (``_on_file_completed``, watcher thread);
        - when a popup opens (``_show_popup``) — a safety net for a file that
          arrived before the OCR pool was ready. (A popup whose file is still
          held back reads it anyway: the popup subscribes its own file to the
          pool, so the file being looked at never waits.)

        Submissions are deduped by the pool, and the cursor is lock-guarded
        because arrivals come from the watcher thread.
        """
        pool = getattr(self, "_ocr_pool", None)
        if pool is None:
            return
        head = None
        pending = None
        with self._ocr_lock:
            if not self._file_order:
                return
            if self._ocr_submitted == 0:
                head = self._file_order[0]
                self._ocr_submitted = 1
            elif self._ocr_released:
                pending = self._file_order[self._ocr_submitted:]
                self._ocr_submitted = len(self._file_order)
            else:
                return  # the first read is still going: batch stays held
        if head is not None:
            # Read the first file alone, then release the batch. The callback
            # fires on a worker thread when the read finishes (immediately
            # when the result is already cached or OCR has no API key).
            self._arm_ocr_first_head_start()
            try:
                pool.submit(head, self._on_first_ocr_done)
            except Exception as exc:
                print(f"[filepicker] OCR submit error: {exc}")
                self._release_ocr_rest()
            return
        for path in pending:
            try:
                pool.submit(path)
            except Exception as exc:
                print(f"[filepicker] OCR submit error: {exc}")

    def _arm_ocr_first_head_start(self) -> None:
        """Start the safety valve for the first (popup) file's read."""
        timer = threading.Timer(_OCR_FIRST_HEAD_START, self._release_ocr_rest)
        timer.daemon = True
        with self._ocr_lock:
            if self._ocr_released:
                return
            self._ocr_head_timer = timer
        timer.start()

    def _on_first_ocr_done(self, _result=None) -> None:
        """The first popup's read finished — send the rest of the batch."""
        self._release_ocr_rest()

    def _release_ocr_rest(self) -> None:
        """Allow the remaining files to be submitted (idempotent)."""
        with self._ocr_lock:
            if self._ocr_released:
                return
            self._ocr_released = True
            timer = self._ocr_head_timer
            self._ocr_head_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._submit_ocr_all()

    # ------------------------------------------------------------------
    def _handle_submit(self, payload: dict) -> None:
        # De-dup: if the exact output filename already exists in the sorted
        # folders, ask the user BEFORE organizing — Skip New File (keep the
        # old file, leave the new download in the watch folder) or Replace
        # Old with New (overwrite). Runs on the main thread while
        # _popup_active is still True, so no other popup can appear above
        # the question dialog.
        request = OrganizeRequest(
            source=payload["file_path"],
            company=payload["company"],
            client=payload["client"],
            site=payload["site"],
            doc_type=payload["doc_type"],
            materials=payload["materials"],
            materials_map=self.config.materials,
            serial=payload["serial"],
            # Financial year read from the document's Delivery Note No. and
            # editable in the popup (None -> today's year).
            fy=payload.get("fy"),
            status=payload["status"],
            root=Path(self.config.root_directory),
            initials_map=self.config.company_initials,
        )
        try:
            existing = [p for p in output_paths(request) if p.exists()]
        except Exception as exc:
            print(f"[filepicker] duplicate check error (proceeding): {exc}")
            existing = []
        if existing:
            choice = ask_duplicate_action(self._root, request.source.name, existing[0])
            if choice != "replace":
                # Skip New File: the old sorted copy wins, and the new
                # download is removed from the watch folder (the popup
                # already released the file, so nothing holds it open).
                if self._delete_original(request.source):
                    self._set_status(
                        f"Skipped — '{existing[0].name}' already exists in "
                        "sorted folders; new download deleted from watch folder."
                    )
                else:
                    self._set_status(
                        f"Skipped — '{existing[0].name}' already exists in "
                        "sorted folders; could not delete the new download "
                        "(still locked) — remove it manually."
                    )
                return
            request.replace = True

        self._organize_active = True

        def run() -> None:
            try:
                self._organize(request)
            finally:
                self._organize_active = False

        threading.Thread(target=run, name="filepicker-organize", daemon=True).start()

    def _handle_skip(self) -> None:
        self._set_status("Skipped — file left untouched in watch folder.")

    def _skip_all_pending(self, current_path: Path) -> List[Path]:
        """Collect and clear every pending popup file (current + queued).

        Called after the popup already closed itself, so the files can be
        deleted. Returns the paths in deletion order (current popup's file
        first, then the remaining queue).
        """
        pending = [Path(p) for p in list(self._popup_queue.queue)]
        try:
            while True:
                self._popup_queue.get_nowait()
        except queue.Empty:
            pass
        current = Path(current_path)
        if current not in pending:
            pending.insert(0, current)
        return pending

    def _handle_skip_all(self, current_path) -> None:
        """"Skip All & Delete": drop every queued popup and remove those files
        from the watch folder.

        The popup that triggered this already released itself (preview closed,
        window destroyed) before calling back — an open popup holds the file
        open on Windows and would block the deletion. Deletion runs on a
        background thread with the same in-use retry as the normal flow.
        """
        paths = self._skip_all_pending(Path(current_path))

        def work() -> None:
            removed = 0
            for p in paths:
                if self._delete_original(p):
                    removed += 1
            if removed == len(paths):
                self._set_status(
                    f"Skipped all — {removed} file(s) removed from watch folder."
                )
            else:
                self._set_status(
                    f"Skipped all — removed {removed}/{len(paths)} file(s) "
                    "(some were still locked; remove them manually)."
                )

        threading.Thread(
            target=work, name="filepicker-skip-all-delete", daemon=True
        ).start()

    def _organize(self, request: OrganizeRequest) -> None:
        source = request.source
        result = organize(request)

        if result.success:
            # Original temporary download is deleted only after all copies
            # succeeded. On Windows the file may briefly be "in use" by another
            # program, so retry for a short while before giving up.
            if not self._delete_original(source):
                return
            # The file was saved — only now may config changes made while
            # filling the popup (new sites/clients/materials/...) be pushed
            # to GitHub ("no config push unless a file is saved").
            try:
                pushed = self.config.flush_pending_push()
                if pushed:
                    print("[filepicker] pushing config additions to GitHub (file saved)")
            except Exception as exc:
                print(f"[filepicker] pending config push error: {exc}")
            n = len(result.destinations)
            self._set_status(f"Saved to {n} folder(s): {', '.join(str(d) for d in result.destinations)}")
        else:
            self._set_status("ERROR: " + "; ".join(result.errors))

    @staticmethod
    def _delete_original(source: Path) -> bool:
        """Delete the watch-folder original, retrying while it is in use.

        Returns True when deleted (or already gone). If it is still locked
        after retries (e.g. another program has it open), the copy has already
        been made, so the user can delete it manually.
        """
        for _ in range(5):
            try:
                source.unlink(missing_ok=True)
                return True
            except PermissionError:
                time.sleep(0.3)  # wait for the lock to be released
            except OSError as exc:
                print(f"[filepicker] could not delete original: {exc}")
                return False
        print(f"[filepicker] original still in use by another program; "
              f"left in watch folder: {source}")
        return False

    def _set_status(self, message: str) -> None:
        print(f"[filepicker] {message}")

    # ------------------------------------------------------------------
    def _is_busy(self) -> bool:
        """True while the app is processing files and must not be interrupted."""
        return (
            self._popup_active
            or self._organize_active
            or not self._popup_queue.empty()
        )

    def _maybe_install_update(self) -> None:
        """Stage an update install once the app is fully idle.

        Called from the main-thread poll loop. A staged update is downloaded
        immediately when detected, but the swap (which replaces the running exe
        and exits) only happens after every file has been processed. The user is
        shown a brief "updating" notice, then the update is applied
        automatically.
        """
        if (self._pending_update is None
                or self._is_busy()
                or self._update_dialog_open):
            return
        update, staged = self._pending_update
        self._update_dialog_open = True
        self._show_updating_dialog(update, staged)

    def _show_updating_dialog(self, update: dict, staged) -> None:
        """Show a brief informational notice, then auto-apply the update."""
        win = ctk.CTkToplevel(self._root)
        win.title(f"FilePicker v{VERSION} — Updating")
        win.geometry("460x200")
        win.configure(fg_color=_BG)
        win.transient(self._root)
        win.attributes("-topmost", True)

        ctk.CTkLabel(
            win, text="🔄 Updating FilePicker",
            font=ctk.CTkFont(size=16, weight="bold"), text_color=_TEXT,
        ).pack(pady=(26, 10))
        ctk.CTkLabel(
            win,
            text=f"Applying version {update['version']}.\n"
                 "The app will restart automatically.",
            font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED, justify="center",
        ).pack(pady=(0, 8))

        def go() -> None:
            win.destroy()
            self._finish_update(update, staged)

        # Auto-apply shortly so the user sees the notice without any click.
        # Closing the window early also triggers the install.
        win.after(_UPDATE_APPLY_DELAY_MS, go)
        win.protocol("WM_DELETE_WINDOW", go)
        win.lift()
        win.focus_force()

    def _finish_update(self, update: dict, staged) -> None:
        """Perform the actual swap + relaunch after the user confirms."""
        self._update_dialog_open = False
        self._pending_update = None
        try:
            from updater import install_update
            print(f"[filepicker] idle; installing update {update['version']}…")
            ok = install_update(update, staged)
            if not ok:
                # install_update aborted (e.g. the exe is locked); never leave
                # the user thinking it worked.
                print(f"[filepicker] update install FAILED ({update['version']}).")
                self._note_update_failure(update["version"])
                self._show_update_failed(update)
        except Exception as exc:
            print(f"[filepicker] install error: {exc}")
            self._note_update_failure(update["version"])
            try:
                self._show_update_failed(update, str(exc))
            except Exception:
                pass

    def _note_update_failure(self, version: str) -> None:
        """Back off after a failed install instead of re-downloading forever."""
        attempts, _next_at = self._update_retries.get(version, (0, 0.0))
        attempts += 1
        delay = _UPDATE_RETRY_BACKOFF[
            min(attempts - 1, len(_UPDATE_RETRY_BACKOFF) - 1)]
        self._update_retries[version] = (attempts, time.monotonic() + delay)
        print(f"[filepicker] update {version} could not be installed "
              f"({attempts}x); next automatic attempt in {delay / 60:.0f} min "
              f"(the tray's Check for updates retries immediately)")

    def _update_retry_blocked(self, version: str) -> Optional[float]:
        """Seconds left before another automatic install attempt (None = now)."""
        entry = self._update_retries.get(version)
        if not entry:
            return None
        _attempts, next_at = entry
        remaining = next_at - time.monotonic()
        return remaining if remaining > 0 else None

    def _show_update_failed(self, update: dict, detail: str = "") -> None:
        """Surface a failed update so it is never a silent no-op."""
        win = ctk.CTkToplevel(self._root)
        win.title(f"FilePicker v{VERSION} — Update Failed")
        win.geometry("480x300")
        win.configure(fg_color=_BG)
        win.transient(self._root)
        win.attributes("-topmost", True)

        msg = (f"Could not install {update['version']}.\n"
               "The app is still on the current version.")
        if detail:
            msg += f"\n\n{detail}"

        ctk.CTkLabel(
            win, text="⚠ Update Failed", font=ctk.CTkFont(size=16, weight="bold"),
            text_color=_DANGER,
        ).pack(pady=(20, 8))
        ctk.CTkLabel(
            win, text=msg, font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED,
            justify="center", wraplength=420,
        ).pack(pady=(0, 10))
        ctk.CTkLabel(
            win,
            text="What to do: quit FilePicker (tray → Quit) and start it again.\n"
                 "Its startup cleanup removes the leftover .old file, and the\n"
                 "next check installs the update.",
            font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
            justify="center", wraplength=420,
        ).pack(pady=(0, 10))
        ctk.CTkButton(
            win, text="OK", width=120, height=34, command=win.destroy,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
        ).pack(pady=(0, 14))
        win.lift()
        win.focus_force()

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._build_root()
        # Finish any interrupted in-place update: the process that applied an
        # update swaps the exe and exits; the freshly launched process must
        # remove the leftover `.old` files and retry file copies that were
        # locked while the old process was still alive.
        try:
            from updater import resume_pending_update
            if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)):
                app_dir = Path(sys.executable).resolve().parent
            else:
                app_dir = Path(__file__).resolve().parent
            resume_pending_update(app_dir)
        except Exception as exc:
            print(f"[filepicker] update resume error: {exc}")
        self._cleanup_old_files()
        self._schedule_old_cleanup_retries()
        self._show_update_notice_if_any()

        # Background OCR pool: EVERY file that lands is read immediately, all
        # together (up to _OCR_MAX simultaneous vision calls), so each popup
        # opens with its fields already filled.
        if self.config.enable_ocr:
            try:
                from ocr import OcrPool
                self._ocr_pool = OcrPool(
                    token=self.config.opencode_token,
                    model=self.config.ocr_model,
                    api_base=self.config.ocr_api_base,
                    # How hard the model may think before answering. Thinking
                    # is ON by default on the DeepSeek API and its chain of
                    # thought is what made a read take 30s+ (see
                    # ocr.OCR_THINKING); "off" is the default, and the read
                    # degrades down a ladder if the gateway refuses the field.
                    thinking=self.config.ocr_thinking,
                )
                if self._ocr_pool.available:
                    self._set_status(
                        f"OCR enabled — every delivery note is read on arrival "
                        f"({_OCR_MAX} at once)")
                else:
                    self._set_status("OCR enabled but no API key found")
                print(f"[filepicker] OCR pool ready (model={self.config.ocr_model}, "
                      f"concurrent={_OCR_MAX}, "
                      f"thinking={self.config.ocr_thinking}, "
                      f"token={'yes' if self._ocr_pool.available else 'MISSING'})")
            except Exception as exc:
                print(f"[filepicker] OCR pool init error: {exc}")
                self._ocr_pool = None
        else:
            # Never silent: a later "OCR didn't run" is diagnosable from the
            # log alone (this flag reads LOCAL config.json only).
            print('[filepicker] OCR disabled — "enable_ocr" is false/missing '
                  "in config.json (set it to true to auto-fill delivery notes)")

        watch_dir = self.config.watch_directory
        self._watcher = None
        self._start_watcher_when_ready(watch_dir)

        self._root.after(_POPUP_POLL_MS, self._poll_popups)
        self._schedule_update_checks()
        # NOTE: no background config pull — the config is fetched only when a
        # popup opens (see popup._start_config_poll). A periodic pull would
        # fight the user edit/push workflow (it kept reverting hand-made
        # deletions and raced the tray push).
        self._start_tray()
        try:
            self._root.mainloop()
        finally:
            watcher = self._watcher
            if watcher is not None:
                watcher.stop()
            self._stop_tray()
            if self._ocr_pool is not None:
                try:
                    self._ocr_pool.shutdown()
                except Exception:
                    pass

    def _start_watcher_when_ready(self, watch_dir: str) -> None:
        """Start the folder watcher as soon as the watch folder exists.

        The watch folder is normally a mapped network drive (``Z:\\Unsorted``)
        and Windows launches auto-start programs BEFORE it reconnects mapped
        drives. The old code called ``mkdir()`` on that path during startup, so
        at login it raised FileNotFoundError, the controller died on the spot
        and the app never came up — which looks exactly like "auto-start does
        not work", while starting it by hand (drive already connected) worked
        perfectly. The watcher is therefore started from a background thread
        that waits for the folder, so the app always comes up and starts
        watching the moment the drive appears.
        """
        def _worker() -> None:
            logged = 0
            for attempt in range(1, _WATCH_DIR_ATTEMPTS + 1):
                try:
                    Path(watch_dir).mkdir(parents=True, exist_ok=True)
                    watcher = DownloadWatcher(
                        watch_directory=watch_dir,
                        on_completed=self._on_file_completed,
                        # How long a finished file waits before its popup
                        # opens (config: popup_delay_seconds, default 1.0s).
                        # The watcher still requires the file to be unlocked,
                        # so a download in progress never pops up early.
                        stable_window=self.config.popup_delay_seconds,
                    )
                    watcher.start()
                except Exception as exc:
                    # Only the first few failures and then an occasional
                    # reminder: a drive that is slow to mount must not fill
                    # the log with the same line every 10 seconds.
                    if logged < 3 or attempt % 30 == 0:
                        print(f"[filepicker] watch folder {watch_dir} is not "
                              f"available yet ({exc}) — retrying "
                              f"({attempt}/{_WATCH_DIR_ATTEMPTS})")
                        logged += 1
                    time.sleep(_WATCH_DIR_RETRY_DELAY)
                    continue
                self._watcher = watcher
                self._set_status(
                    f"Watching {watch_dir} for completed downloads…")
                print(f"[filepicker] watching {watch_dir} for completed "
                      f"downloads (attempt {attempt})")
                return
            print(f"[filepicker] gave up waiting for {watch_dir} after "
                  f"{_WATCH_DIR_ATTEMPTS * _WATCH_DIR_RETRY_DELAY / 60:.0f} "
                  f"minutes — check that the drive is connected, then restart "
                  f"FilePicker")
            self._set_status(f"Watch folder {watch_dir} is unavailable — "
                             f"restart FilePicker once the drive is connected")

        threading.Thread(target=_worker, daemon=True,
                         name="filepicker-watch-start").start()

    # ------------------------------------------------------------------
    # Tray icon + manual update
    # ------------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            from tray import TrayIcon
            self._tray = TrayIcon(
                on_check_update=self._tray_check_update,
                on_quit=self._tray_quit,
                on_force_sync=self._tray_force_sync,
                on_force_push=self._tray_force_push,
                on_toggle_startup=self._tray_toggle_startup,
                startup_enabled=self._auto_start_enabled,
            )
            self._tray.start()
        except Exception as exc:
            print(f"[filepicker] tray start error: {exc}")

    def _auto_start_enabled(self) -> bool:
        """Whether auto-start is switched on (tray menu label; never raises)."""
        try:
            return bool(self.config.auto_start)
        except Exception:
            return False

    def _stop_tray(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass

    def _tray_check_update(self) -> None:
        # Called from the pystray thread; marshal onto the Tk main thread.
        self._ui_commands.put("check_update")

    def _tray_force_sync(self) -> None:
        # Called from the pystray thread; marshal onto the Tk main thread.
        self._ui_commands.put("force_sync")

    def _tray_force_push(self) -> None:
        # Called from the pystray thread; marshal onto the Tk main thread.
        self._ui_commands.put("push_config")

    def _tray_toggle_startup(self) -> None:
        # Called from the pystray thread; marshal onto the Tk main thread.
        self._ui_commands.put("toggle_startup")

    def _toggle_auto_start(self) -> None:
        """Tray → turn "launch at Windows login" on or off.

        Writes the choice to config.json and immediately installs/removes the
        Windows entries, so the tray label (which reads the live state) is
        never out of step with reality.
        """
        try:
            import startup
        except Exception as exc:
            self._set_status(f"Auto-start unavailable: {exc}")
            return
        wanted = not self.config.auto_start
        self.config.set_auto_start(wanted)
        if wanted:
            ok = startup.install() and startup.verify()
            self._set_status(
                "Auto-start at login: ON" if ok
                else "Auto-start could NOT be enabled — see the log")
        else:
            startup.remove()
            self._set_status("Auto-start at login: OFF")
        print(f"[filepicker] auto-start toggled to {wanted} ({startup.state()})")

    def _tray_quit(self) -> None:
        self._ui_commands.put("quit")

    def _force_sync_now(self) -> None:
        """Manual "Force sync with repo" from the tray.

        Replaces the local catalog with the GitHub config (deletions
        included) on a background thread, then refreshes the open popup if
        anything changed.
        """
        def work() -> None:
            try:
                sync_result = self.config.force_sync_from_github(timeout=10.0)
                if sync_result is None:
                    print("[filepicker] force sync FAILED (could not fetch repo config)")
                    return
                if not sync_result:
                    print("[filepicker] force sync: local config already matches repo")
                    return

                def done() -> None:
                    popup = getattr(self, "_current_popup", None)
                    if popup is not None:
                        try:
                            if popup.window.winfo_exists():
                                popup.refresh_from_config()
                        except Exception as exc:
                            print(f"[filepicker] force sync refresh error: {exc}")
                    print("[filepicker] force sync applied: local catalog now matches repo")

                try:
                    self._root.after(0, done)
                except Exception:
                    pass
            except Exception as exc:
                print(f"[filepicker] force sync error: {exc}")

        threading.Thread(target=work, name="filepicker-force-sync", daemon=True).start()

    def _push_config_now(self) -> None:
        """Manual "Push local config to GitHub" from the tray.

        The opposite of Force sync: deletes what is on GitHub and replaces it
        with THIS machine's local config (current sites, clients, companies,
        materials, doc types — deletions included) on a background thread.
        Failures are surfaced in a dialog so a refused push is never a
        silent "nothing happened".
        """
        def work() -> None:
            try:
                ok = self.config.force_push_to_github(
                    reason="FilePicker: tray push — replace remote with local"
                )
                if ok:
                    self._set_status(
                        "Pushed local config to GitHub (remote replaced)."
                    )
                else:
                    self._set_status(
                        "Push FAILED — see the log for the reason "
                        "(token / enable_github_push / empty config)."
                    )

                    def show_error() -> None:
                        try:
                            import tkinter.messagebox as mb
                            mb.showerror(
                                "FilePicker — Config Push Failed",
                                "The local config could NOT be pushed to "
                                "GitHub.\n\n"
                                "Check FilePicker.log for the exact reason "
                                "(missing FILEPICKER_GITHUB_TOKEN / "
                                "github_token.txt, enable_github_push set to "
                                "false, or an empty/broken local config.json).",
                                parent=self._root,
                            )
                        except Exception:
                            pass

                    try:
                        self._root.after(0, show_error)
                    except Exception:
                        pass
            except Exception as exc:
                print(f"[filepicker] tray push error: {exc}")

        threading.Thread(target=work, name="filepicker-tray-push", daemon=True).start()

    def _check_update_now(self) -> None:
        """Manual 'Check for updates' from the tray (runs on the main thread).

        A manual check always tries immediately: it clears any install-failure
        backoff for the version it finds, so the user is never told to wait.
        """
        try:
            from updater import check_for_update, download_update
            update = check_for_update(strict=False)
            if not update:
                self._set_status("Already up to date.")
                return
            self._update_retries.pop(update["version"], None)
            staged = download_update(update)
            if staged:
                self._pending_update = (update, staged)
                print(f"[filepicker] update {update['version']} downloaded; installing…")
                self._maybe_install_update()
            else:
                self._set_status("Update download failed.")
        except Exception as exc:
            print(f"[filepicker] manual update error: {exc}")

    def _schedule_update_checks(self) -> None:
        """Check for updates periodically, without blocking the UI.

        The network check (and download) run on a background thread so the
        popup loop never stalls. Only when a genuinely newer build is found do
        we stage it and (once idle) show the updating dialog — never when the
        app is already on the latest version.
        """
        try:
            from updater import CHECK_INTERVAL, check_for_update, download_update

            def check() -> None:
                # If an update is already staged and waiting for idle, skip
                # re-checking until it has been installed.
                if self._pending_update is not None:
                    try:
                        self._root.after(int(CHECK_INTERVAL * 1000), check)
                    except Exception:
                        pass
                    return

                def work() -> None:
                    try:
                        update = check_for_update()
                        if not update:
                            return
                        blocked = self._update_retry_blocked(update["version"])
                        if blocked is not None:
                            print(f"[filepicker] update {update['version']} is in "
                                  f"backoff after a failed install; next attempt "
                                  f"in {blocked / 60:.0f} min")
                            return
                        staged = download_update(update)
                        if staged:
                            self._root.after(0, lambda: self._stage_update(update, staged))
                    except Exception as exc:
                        print(f"[filepicker] updater check error: {exc}")

                threading.Thread(target=work, name="filepicker-updater", daemon=True).start()
                try:
                    self._root.after(int(CHECK_INTERVAL * 1000), check)
                except Exception:
                    pass

            self._root.after(1000, check)  # first check shortly after start
        except Exception as exc:
            print(f"[filepicker] updater unavailable: {exc}")

    def _stage_update(self, update: dict, staged) -> None:
        """Record a staged update (main thread) and apply once idle."""
        self._pending_update = (update, staged)
        print(
            f"[filepicker] update {update['version']} downloaded; "
            "will install once all files are processed"
        )


def main() -> None:
    # Handle one-shot CLI flags before file logging so console output is visible
    # (file logging redirects stdout to FilePicker.log).
    args = sys.argv[1:]
    if "--install-startup" in args:
        from startup import install
        print("Auto-start installed." if install() else "Failed to install auto-start.")
        return
    if "--remove-startup" in args:
        from startup import remove
        print("Auto-start removed." if remove() else "Failed to remove auto-start.")
        return
    if "--check-startup" in args:
        from startup import verify
        print("Auto-start verified and ready." if verify()
              else "Auto-start NOT working (shortcut missing or stale).")
        return
    if "--push-config" in args:
        cfg = ConfigManager()
        cfg.load()
        print(f"Push enabled: {cfg._github_push_enabled()} (live={cfg.enable_live_config}, push={cfg.enable_github_push})")
        ok = cfg.push_to_github(reason="FilePicker: manual --push-config")
        print("Push succeeded." if ok else "Push skipped/failed (check token and enable_github_push).")
        return

    _setup_file_logging()
    print(f"[filepicker] start v{VERSION} args={args} exe={sys.executable} cwd={os.getcwd()}")

    config = ConfigManager()

    # First run = the config file doesn't exist yet. Show a one-time setup
    # dialog asking for the watch/root directories, pre-filled with defaults.
    first_run = not config.path.exists()
    config.load()
    if first_run:
        try:
            from setup import run_first_time_setup
            run_first_time_setup(config)
        except Exception as exc:
            print(f"[filepicker] first-run setup error: {exc}")

    # Verify auto-start will actually work at next login (unless disabled in
    # config.json): the per-user Run key (primary) and/or the Startup-folder
    # shortcut must exist, point at the currently running app, and their
    # target must still exist — otherwise reinstall. Retried a few times
    # because at login the user profile is still settling. Runs in a
    # background thread so it never delays startup, and logs the state before
    # AND after so "it does not start with Windows" is answerable from
    # FilePicker.log alone.
    if config.auto_start:
        try:
            import startup

            def _ensure_startup() -> None:
                print(f"[filepicker] auto-start check: {startup.state()}")
                for attempt in range(1, _STARTUP_ATTEMPTS + 1):
                    if startup.ensure():
                        print("[filepicker] auto-start OK — FilePicker will "
                              "launch at Windows login")
                        return
                    if attempt < _STARTUP_ATTEMPTS:
                        time.sleep(_STARTUP_RETRY_DELAY)
                print("[filepicker] auto-start FAILED — FilePicker will NOT "
                      "launch at login. Use the tray menu (Auto-start at "
                      "login) or run: FilePicker.exe --install-startup")

            threading.Thread(target=_ensure_startup, daemon=True,
                             name="filepicker-startup").start()
        except Exception as exc:
            print(f"[filepicker] auto-start setup failed: {exc}")
    else:
        print("[filepicker] auto-start is switched off in config.json "
              '("auto_start": false)')

    # Config changes made from a popup (new sites/clients/...) are pushed to
    # GitHub ONLY after a file is actually saved (see _organize) or via the
    # tray's manual "Push local config to GitHub" — never at startup.

    controller = FilePickerController(config)
    try:
        controller.run()
    except Exception as exc:
        import traceback

        msg = f"FilePicker crashed: {exc}\n{traceback.format_exc()}"
        print(msg)
        try:
            import tkinter.messagebox as mb

            mb.showerror("FilePicker — Crash", msg[:2000])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
