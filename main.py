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

# Ensure the app's own folder is importable no matter the working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import customtkinter as ctk

from config import ConfigManager
from organizer import OrganizeRequest, organize
from popup import FilePickerPopup
from version import VERSION
from watcher import DownloadWatcher


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

# How often to poll GitHub for the live config (seconds). With cache-busting
# a push shows up within one interval even if the popup is already open.
_CONFIG_SYNC_INTERVAL = 30


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
        self._ui_commands: "queue.Queue[str]" = queue.Queue()  # tray -> main thread
        self._tray = None
        self._root = None
        self._current_popup = None  # the popup currently on screen (so live config can refresh it)

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
        self._root.after(100, self._poll_popups)

    def _show_popup(self, path: Path) -> None:
        popup = FilePickerPopup(
            config=self.config,
            file_path=path,
            on_submit=self._handle_submit,
            on_skip=self._handle_skip,
        )
        self._current_popup = popup
        try:
            # Blocking until the modal is dismissed.
            popup.show()
        finally:
            self._current_popup = None

    # ------------------------------------------------------------------
    def _handle_submit(self, payload: dict) -> None:
        self._organize_active = True

        def run() -> None:
            try:
                self._organize(payload)
            finally:
                self._organize_active = False

        threading.Thread(target=run, name="filepicker-organize", daemon=True).start()

    def _handle_skip(self) -> None:
        self._set_status("Skipped — file left untouched in watch folder.")

    def _organize(self, payload: dict) -> None:
        source: Path = payload["file_path"]
        request = OrganizeRequest(
            source=source,
            company=payload["company"],
            client=payload["client"],
            site=payload["site"],
            doc_type=payload["doc_type"],
            materials=payload["materials"],
            materials_map=self.config.materials,
            serial=payload["serial"],
            status=payload["status"],
            root=Path(self.config.root_directory),
            initials_map=self.config.company_initials,
        )
        result = organize(request)

        if result.success:
            # Original temporary download is deleted only after all copies
            # succeeded. On Windows the file may briefly be "in use" by another
            # program, so retry for a short while before giving up.
            if not self._delete_original(source):
                return
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
                self._show_update_failed(update)
        except Exception as exc:
            print(f"[filepicker] install error: {exc}")
            try:
                self._show_update_failed(update, str(exc))
            except Exception:
                pass

    def _show_update_failed(self, update: dict, detail: str = "") -> None:
        """Surface a failed update so it is never a silent no-op."""
        win = ctk.CTkToplevel(self._root)
        win.title(f"FilePicker v{VERSION} — Update Failed")
        win.geometry("460x220")
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
        ).pack(pady=(24, 8))
        ctk.CTkLabel(
            win, text=msg, font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED,
            justify="center", wraplength=400,
        ).pack(pady=(0, 12))
        ctk.CTkButton(
            win, text="OK", width=120, height=34, command=win.destroy,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
        ).pack(pady=(0, 16))
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
        self._show_update_notice_if_any()

        watch_dir = self.config.watch_directory
        watcher = DownloadWatcher(
            watch_directory=watch_dir,
            on_completed=self._on_file_completed,
        )
        watcher.start()
        self._set_status(f"Watching {watch_dir} for completed downloads…")

        self._root.after(100, self._poll_popups)
        self._schedule_update_checks()
        self._schedule_config_sync()
        self._start_tray()
        try:
            self._root.mainloop()
        finally:
            watcher.stop()
            self._stop_tray()

    # ------------------------------------------------------------------
    # Tray icon + manual update
    # ------------------------------------------------------------------
    def _start_tray(self) -> None:
        try:
            from tray import TrayIcon
            self._tray = TrayIcon(
                on_check_update=self._tray_check_update,
                on_quit=self._tray_quit,
            )
            self._tray.start()
        except Exception as exc:
            print(f"[filepicker] tray start error: {exc}")

    def _stop_tray(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass

    def _tray_check_update(self) -> None:
        # Called from the pystray thread; marshal onto the Tk main thread.
        self._ui_commands.put("check_update")

    def _tray_quit(self) -> None:
        self._ui_commands.put("quit")

    def _check_update_now(self) -> None:
        """Manual 'Check for updates' from the tray (runs on the main thread)."""
        try:
            from updater import check_for_update, download_update
            update = check_for_update(strict=False)
            if not update:
                self._set_status("Already up to date.")
                return
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
                        if update:
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

    def _schedule_config_sync(self) -> None:
        """Poll GitHub for live config every :data:`_CONFIG_SYNC_INTERVAL` seconds.

        Works even while a popup is open: the network fetch runs on a background
        thread and, if the config changed, refreshes the open popup in place
        (preserving what the user is typing) via ``popup.refresh_from_config()``.
        Disabled when ``enable_live_config`` is false in config.json.
        """
        def check() -> None:
            def work() -> None:
                try:
                    if not self.config.enable_live_config:
                        return
                    changed = self.config.sync_from_github(timeout=5.0)
                    if changed:
                        def do_refresh() -> None:
                            popup = getattr(self, "_current_popup", None)
                            if popup is not None:
                                try:
                                    if popup.window.winfo_exists():
                                        popup.refresh_from_config()
                                except Exception as exc:
                                    print(f"[filepicker] live config refresh error: {exc}")
                            print("[filepicker] live config updated from GitHub (background)")

                        try:
                            self._root.after(0, do_refresh)
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"[filepicker] config sync error: {exc}")
                finally:
                    try:
                        self._root.after(int(_CONFIG_SYNC_INTERVAL * 1000), check)
                    except Exception:
                        pass

            threading.Thread(target=work, name="filepicker-config-sync", daemon=True).start()

        try:
            self._root.after(2000, check)
        except Exception as exc:
            print(f"[filepicker] config sync unavailable: {exc}")


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
    # config.json): the Startup shortcut must exist, point at the currently
    # running app, and its target must still exist — otherwise reinstall it.
    # Runs in a background thread so it never delays startup.
    if config.auto_start:
        try:
            from startup import ensure, verify

            def _ensure_startup() -> None:
                if verify():
                    print("[filepicker] Auto-start verified.")
                    return
                print("[filepicker] Auto-start shortcut missing or stale; reinstalling…")
                ok = ensure()
                print("[filepicker] Auto-start repaired." if ok
                      else "[filepicker] Auto-start repair FAILED.")

            threading.Thread(target=_ensure_startup, daemon=True).start()
        except Exception as exc:
            print(f"[filepicker] auto-start setup failed: {exc}")

    # If this machine has local catalog changes not yet on GitHub (e.g. a site
    # added via "Add Site" before push was enabled, like the "tirth" entry),
    # push them now so every other machine sees them within 30s.
    try:
        if config._github_push_enabled():
            def _startup_push() -> None:
                time.sleep(4)
                try:
                    if config.push_to_github(reason="FilePicker: startup sync"):
                        print("[filepicker] startup sync pushed local catalog to GitHub")
                except Exception as exc:
                    print(f"[filepicker] startup push failed: {exc}")

            threading.Thread(target=_startup_push, name="filepicker-startup-push", daemon=True).start()
    except Exception as exc:
        print(f"[filepicker] startup sync check failed: {exc}")

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
