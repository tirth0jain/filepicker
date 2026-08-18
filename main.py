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
import sys
import threading
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


# Popup palette (matches popup.py / viewer.py).
_BG = "#15151d"
_BG_SECONDARY = "#1f1f2b"
_ACCENT = "#5b8cff"
_ACCENT_HOVER = "#3f6fe0"
_TEXT = "#f2f2f7"
_TEXT_MUTED = "#b6b6c9"


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
        self._root = None

    # ------------------------------------------------------------------
    def _build_root(self) -> None:
        self._root = ctk.CTk()
        self._root.withdraw()  # hidden background window
        self._root.title(f"FilePicker v{VERSION}")
        self._root.protocol("WM_DELETE_WINDOW", self._root.destroy)

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
        self._root.after(100, self._poll_popups)

    def _show_popup(self, path: Path) -> None:
        popup = FilePickerPopup(
            config=self.config,
            file_path=path,
            on_submit=self._handle_submit,
            on_skip=self._handle_skip,
        )
        # Blocking until the modal is dismissed.
        popup.show()

    # ------------------------------------------------------------------
    def _handle_submit(self, payload: dict) -> None:
        def run() -> None:
            self._organize(payload)
        threading.Thread(target=run, name="filepicker-organize", daemon=True).start()

    def _handle_skip(self) -> None:
        self._set_status("Skipped — file left untouched in watch folder.")

    def _organize(self, payload: dict) -> None:
        source: Path = payload["file_path"]
        request = OrganizeRequest(
            source=source,
            company=payload["company"],
            site=payload["site"],
            doc_type=payload["doc_type"],
            materials=payload["materials"],
            materials_map=self.config.materials,
            serial=payload["serial"],
            status=payload["status"],
            root=Path(self.config.root_directory),
        )
        result = organize(request)

        if result.success:
            # Original temporary download is deleted only after all copies
            # succeeded.
            try:
                source.unlink(missing_ok=True)
            except OSError as exc:
                self._set_status(f"Organized but could not delete original: {exc}")
                return
            n = len(result.destinations)
            self._set_status(f"Saved to {n} folder(s): {', '.join(str(d) for d in result.destinations)}")
        else:
            self._set_status("ERROR: " + "; ".join(result.errors))

    def _set_status(self, message: str) -> None:
        print(f"[filepicker] {message}")

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._build_root()
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
        try:
            self._root.mainloop()
        finally:
            watcher.stop()

    def _schedule_update_checks(self) -> None:
        """Check for updates at startup and periodically (non-blocking)."""
        try:
            from updater import run_update_check, schedule_periodic
            run_update_check()
            schedule_periodic(self._root)
        except Exception as exc:
            print(f"[filepicker] updater unavailable: {exc}")


def main() -> None:
    # Handle one-shot CLI flags before starting the background app.
    args = sys.argv[1:]
    if "--install-startup" in args:
        from startup import install
        print("Auto-start installed." if install() else "Failed to install auto-start.")
        return
    if "--remove-startup" in args:
        from startup import remove
        print("Auto-start removed." if remove() else "Failed to remove auto-start.")
        return

    config = ConfigManager()
    config.load()

    # Auto-register for Windows startup on first run (unless disabled in
    # config.json). Runs in a background thread so it never delays startup.
    if config.auto_start:
        try:
            from startup import install, is_installed
            if not is_installed():
                threading.Thread(target=install, daemon=True).start()
        except Exception as exc:
            print(f"[filepicker] auto-start setup failed: {exc}")

    controller = FilePickerController(config)
    controller.run()


if __name__ == "__main__":
    main()
