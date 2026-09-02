"""System tray icon for FilePicker.

The app runs hidden in the background, so the tray icon is the only persistent
user-facing control. It exposes a "Check for updates" action (a manual update
trigger) and a "Quit" option. pystray runs its icon in a background thread;
menu actions are forwarded to the controller, which marshals them onto the Tk
main thread via a thread-safe command queue.
"""

from __future__ import annotations

import threading

from PIL import Image, ImageDraw

_ACCENT = (91, 140, 255)


def _build_icon_image() -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=_ACCENT)
    # A simple document glyph.
    d.rounded_rectangle([20, 16, 44, 48], radius=3, fill=(255, 255, 255))
    d.rectangle([24, 26, 40, 30], fill=_ACCENT)
    d.rectangle([24, 34, 40, 38], fill=_ACCENT)
    return img


class TrayIcon:
    """Owns the pystray icon and forwards menu actions to callbacks."""

    def __init__(self, on_check_update, on_quit, on_force_sync=None) -> None:
        self._on_check_update = on_check_update
        self._on_quit = on_quit
        self._on_force_sync = on_force_sync
        self._icon = None

    def start(self) -> None:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except ImportError:
            print("[filepicker] pystray not installed; tray icon disabled.")
            return
        self._icon = pystray.Icon(
            "FilePicker",
            icon=_build_icon_image(),
            title="FilePicker",
            menu=Menu(
                MenuItem("Check for updates", self._check_update),
                MenuItem("Force sync with repo", self._force_sync),
                MenuItem("Quit", self._quit),
            ),
        )
        threading.Thread(
            target=self._icon.run, name="filepicker-tray", daemon=True
        ).start()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def _check_update(self, _icon, _item) -> None:
        self._on_check_update()

    def _force_sync(self, _icon, _item) -> None:
        if self._on_force_sync is not None:
            self._on_force_sync()

    def _quit(self, _icon, _item) -> None:
        self._on_quit()
