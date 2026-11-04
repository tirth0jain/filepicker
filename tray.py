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

    def __init__(self, on_check_update, on_quit, on_force_sync=None,
                 on_force_push=None, on_toggle_startup=None,
                 on_change_watch=None, startup_enabled=None,
                 watch_label=None, shared_install=False,
                 ocr_models=None, ocr_model=None,
                 on_set_ocr_model=None) -> None:
        self._on_check_update = on_check_update
        self._on_quit = on_quit
        self._on_force_sync = on_force_sync
        self._on_force_push = on_force_push
        self._on_toggle_startup = on_toggle_startup
        self._on_change_watch = on_change_watch
        self._startup_enabled = startup_enabled
        self._watch_label = watch_label
        self._shared_install = bool(shared_install)
        # Which vision model reads delivery notes. Deliberately in the tray:
        # the app has no window of its own, so switching a model must not
        # require hand-editing config.json and restarting.
        self._ocr_models = list(ocr_models or [])
        self._ocr_model = ocr_model
        self._on_set_ocr_model = on_set_ocr_model
        self._icon = None

    def _startup_label(self, _item=None) -> str:
        """Menu label that always shows the live auto-start state.

        pystray re-evaluates a callable label every time the menu opens, so
        the entry can never claim "On" while the Windows entry is missing.
        """
        enabled = False
        if self._startup_enabled is not None:
            try:
                enabled = bool(self._startup_enabled())
            except Exception:
                enabled = False
        return f"Auto-start at login: {'On' if enabled else 'Off'}"

    def _watch_text(self, _item=None) -> str:
        """Menu label showing the folder THIS copy watches (live)."""
        folder = ""
        if self._watch_label is not None:
            try:
                folder = str(self._watch_label())
            except Exception:
                folder = ""
        return f"Watching: {folder}" if folder else "Watching: (not set)"

    # -- OCR model submenu ------------------------------------------------
    def _current_model(self) -> str:
        """The model id in force right now (never raises)."""
        if self._ocr_model is None:
            return ""
        try:
            return str(self._ocr_model())
        except Exception:
            return ""

    def _model_text(self, entry) -> str:
        """Menu label for one catalog entry.

        Two providers can serve the same model id, so the provider stays in
        the label: "GLM-5.3 Flash (CommandCode)" leaves no doubt which gateway
        the read will hit.
        """
        if isinstance(entry, dict):
            label = str(entry.get("label") or entry.get("id") or "")
            provider = str(entry.get("provider_label")
                           or entry.get("provider") or "")
        else:
            label, provider = str(entry), ""
        return f"{label} ({provider})" if provider else label

    def _ocr_text(self, _item=None) -> str:
        """Submenu title that always shows the model in force."""
        current = self._current_model()
        for entry in self._ocr_models:
            if isinstance(entry, dict) and entry.get("id") == current:
                return f"OCR model: {self._model_text(entry)}"
        return f"OCR model: {current or 'default'}"

    def _model_checked(self, model_id):
        return lambda _item: self._current_model() == model_id

    def _model_action(self, model_id):
        def _choose(_icon, _item) -> None:
            if self._on_set_ocr_model is not None:
                self._on_set_ocr_model(model_id)
        return _choose

    def _ocr_menu(self):
        """The "OCR model" submenu, or None when there is nothing to offer."""
        try:
            from pystray import Menu, MenuItem
        except Exception:
            return None
        entries = []
        for entry in self._ocr_models:
            model_id = entry.get("id") if isinstance(entry, dict) else entry
            if not model_id:
                continue
            entries.append(MenuItem(
                self._model_text(entry),
                self._model_action(model_id),
                checked=self._model_checked(model_id),
                radio=True,
            ))
        if not entries:
            return None
        return MenuItem(self._ocr_text, Menu(*entries))

    def start(self) -> None:
        try:
            import pystray
            from pystray import Menu, MenuItem
        except Exception as exc:
            # Not just ImportError: on a headless machine pystray imports but
            # cannot reach a display, and that must never kill the app.
            print(f"[filepicker] tray icon disabled: {exc}")
            return
        items = [
            # Which folder this person watches — always visible, because on a
            # shared install "why did/didn't I get a popup?" is answered here.
            MenuItem(self._watch_text, None, enabled=False),
            MenuItem("Change watch folder…", self._change_watch),
        ]
        # Which model reads the delivery notes. Placed high in the menu
        # because it is the one setting people actually change (a slower model
        # when reads come back wrong, a faster one when they are fine).
        ocr_menu = self._ocr_menu()
        if ocr_menu is not None:
            items.append(ocr_menu)
        items += [
            MenuItem("Check for updates", self._check_update),
            MenuItem(self._startup_label, self._toggle_startup),
        ]
        if self._shared_install:
            # The shared config.json IS the live config, so the GitHub actions
            # are meaningless here (and a stray push once replaced the real
            # catalog with a test one).
            items.append(MenuItem("Config: shared folder (GitHub sync off)",
                                  None, enabled=False))
        else:
            items.append(MenuItem("Force sync from repo (overwrite local)",
                                  self._force_sync))
            items.append(MenuItem("Push local config to GitHub (replace remote)",
                                  self._force_push))
        items.append(MenuItem("Quit", self._quit))
        self._icon = pystray.Icon(
            "FilePicker",
            icon=_build_icon_image(),
            title="FilePicker",
            menu=Menu(*items),
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

    def refresh(self) -> None:
        """Re-evaluate the callable labels/ticks now (model or folder changed)."""
        if self._icon is None:
            return
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _check_update(self, _icon, _item) -> None:
        self._on_check_update()

    def _force_sync(self, _icon, _item) -> None:
        if self._on_force_sync is not None:
            self._on_force_sync()

    def _force_push(self, _icon, _item) -> None:
        if self._on_force_push is not None:
            self._on_force_push()

    def _toggle_startup(self, _icon, _item) -> None:
        if self._on_toggle_startup is not None:
            self._on_toggle_startup()

    def _change_watch(self, _icon, _item) -> None:
        if self._on_change_watch is not None:
            self._on_change_watch()

    def _quit(self, _icon, _item) -> None:
        self._on_quit()
