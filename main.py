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
from pathlib import Path

# Ensure the app's own folder is importable no matter the working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import customtkinter as ctk

from config import ConfigManager
from organizer import OrganizeRequest, organize
from popup import FilePickerPopup
from watcher import DownloadWatcher


class FilePickerController:
    """Owns the hidden root window, the watcher and the popup flow."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._popup_queue: "queue.Queue[Path]" = queue.Queue()
        self._popup_active = False
        self._root = None

        # Progress/log status shown in the (mostly hidden) root.
        self._status_var = None

    # ------------------------------------------------------------------
    def _build_root(self) -> None:
        self._root = ctk.CTk()
        self._root.withdraw()  # hidden background window
        self._root.title("FilePicker")
        self._root.protocol("WM_DELETE_WINDOW", self._root.destroy)

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
                self._show_popup(path)
        self._root.after(200, self._poll_popups)

    def _show_popup(self, path: Path) -> None:
        popup = FilePickerPopup(
            config=self.config,
            file_path=path,
            on_submit=self._handle_submit,
            on_skip=self._handle_skip,
        )
        # Blocking until the modal is dismissed.
        popup.show()
        self._popup_active = False

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

        watch_dir = self.config.watch_directory
        watcher = DownloadWatcher(
            watch_directory=watch_dir,
            on_completed=self._on_file_completed,
        )
        watcher.start()
        self._set_status(f"Watching {watch_dir} for completed downloads…")

        self._root.after(200, self._poll_popups)
        try:
            self._root.mainloop()
        finally:
            watcher.stop()


def main() -> None:
    config = ConfigManager()
    config.load()
    controller = FilePickerController(config)
    controller.run()


if __name__ == "__main__":
    main()
