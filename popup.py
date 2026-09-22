"""The FilePicker popup dialog.

A top-most modal window that appears when a completed download is detected. It
captures the metadata needed to rename and route the file:

- Company / Client / Site (with an inline "Add New Site" and "Add New Company")
- Document Type
- Material multi-select (with "Add Material")
- Serial number
- Received Copy checkbox (drives status)
- Save & Organize / Skip buttons
"""

from __future__ import annotations

import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

import filename as fn
import winfocus
from config import ConfigManager
from version import VERSION

# Sentinel options shown at the bottom of the Site / Company dropdowns.
ADD_NEW_SITE_OPTION = "[+ Add New Site...]"
ADD_NEW_CLIENT_OPTION = "[+ Add New Client...]"
ADD_NEW_COMPANY_OPTION = "[+ Add New Company...]"
ADD_NEW_MATERIAL_OPTION = "[+ Add Material...]"

# Maximum number of matches shown at once in the searchable dropdown. The list
# is live-filtered as the user types, so a long client/site list only ever
# shows the first few closest matches instead of dumping the entire menu.
_MAX_DROPDOWN_RESULTS = 5

# Material chips panel: at most this many rows of chips are visible at once;
# any extra rows scroll inside the panel (a large catalog must never push the
# Serial number / Save buttons out of the fixed-height popup window).
_MATERIAL_ROWS_VISIBLE = 5
# Nominal row pitch for the panel height (28px chip + 6px gap + slack).
_MATERIAL_ROW_PITCH = 36
# Compact layout (short screens / scaled-up displays): tighter chip pitch,
# widget heights and paddings so the Serial number, the Received Copy
# checkbox, the filename preview and the Save/Skip buttons ALL fit.
_MATERIAL_ROW_PITCH_COMPACT = 30
# Rough height (unscaled px) the form needs before widgets start getting
# squeezed. Below it — or on a display scaled up by DPI settings — the popup
# switches to the compact spacing.
_FORM_H_COMFORT = 790

# How often the "OCR: reading document…" line refreshes its live counters
# ("12 files read together") while this file's read is still in flight. Every
# download is read simultaneously, so the counters show the batch is being
# read together rather than this one file being stuck "processing".
_OCR_PROGRESS_MS = 500

# When a new popup is checked for actually being on screen (see
# FilePickerPopup._ensure_popup_visible). Late enough for CustomTkinter's
# withdraw/re-show titlebar dance to have finished, early enough that a
# hidden popup is repaired before the user wonders where it went.
_POPUP_VISIBLE_CHECK_MS = 1200


def material_display_order(materials_map, selected) -> List:
    """Chip display order for a popup: selected materials move to the top.

    Cosmetic only — the underlying map/config order is never changed. Each
    group (selected first, then the rest) is sorted alphabetically by the
    material's shortcode, so deselecting returns a chip to its sorted spot.
    """
    items = sorted(
        materials_map.items(),
        key=lambda kv: (str(kv[1]).upper(), str(kv[0]).lower()),
    )
    sel = [it for it in items if it[0] in selected]
    rest = [it for it in items if it[0] not in selected]
    return sel + rest


def alt_seq_step(pending: str, keysym: str) -> tuple:
    """One Alt+<key> step of the material hotkey chord (Alt+AL toggles Aluminium).

    Returns ``(new_pending, matched_code)``: the new pending letter sequence
    and, once two letters form a complete code, that code (the caller toggles
    the material and starts fresh). Non-letter keys never affect the chord.
    """
    if len(keysym) != 1 or not keysym.isalpha():
        return pending, None
    seq = (pending + keysym.lower())[-2:]
    if len(seq) < 2:
        return seq, None
    return "", seq.upper()


# Outline drawn around the option the arrow keys currently point at (see
# _wire_dialog_choices). Every question dialog shows its options' letters in
# the button label, so the keyboard route is discoverable without a manual.
_DIALOG_SELECT_BORDER = 3


def _wire_dialog_choices(dialog, buttons, letters, default_index: int = 0) -> dict:
    """Make a question dialog's options selectable by keyboard.

    Two ways to answer, both acting immediately (no confirmation step):

    - the arrow keys move a visible selection (Up/Left = previous, Down/Right
      = next, wrapping) and Enter/Space presses the selected option;
    - **Ctrl+<letter>** presses the option whose letter that is, wherever the
      focus is inside the dialog — one letter always, never a chord.

    *buttons* are the CTkButtons in visual order and *letters* the matching
    single letters (the same ones shown in their labels). The selected option
    is outlined so the arrows have something visible to move; the caller keeps
    its own bindings (Escape, Ctrl+S, ...). Returns the selection state dict.
    """
    count = len(buttons)
    if not count:
        return {"index": 0}
    state = {"index": max(0, min(int(default_index), count - 1))}

    def render() -> None:
        for i, btn in enumerate(buttons):
            try:
                btn.configure(
                    border_width=_DIALOG_SELECT_BORDER if i == state["index"] else 0,
                    border_color=_TEXT if i == state["index"] else _BG_FIELD,
                )
            except Exception:
                pass
        try:
            buttons[state["index"]].focus_set()
        except Exception:
            pass

    def move(step: int) -> None:
        state["index"] = (state["index"] + step) % count
        render()

    def activate() -> None:
        try:
            buttons[state["index"]].invoke()
        except Exception:
            pass

    for seq, handler in (
        ("<Left>", lambda _e: move(-1)),
        ("<Up>", lambda _e: move(-1)),
        ("<Right>", lambda _e: move(1)),
        ("<Down>", lambda _e: move(1)),
        ("<Return>", lambda _e: activate()),
        ("<KP_Enter>", lambda _e: activate()),
        ("<space>", lambda _e: activate()),
    ):
        try:
            dialog.bind(seq, handler)
        except Exception:
            pass

    for index, letter in enumerate(letters):
        if not letter or index >= count:
            continue
        for seq in (f"<Control-{letter.lower()}>", f"<Control-{letter.upper()}>"):
            try:
                dialog.bind(seq, lambda _e, i=index: buttons[i].invoke())
            except Exception:
                continue

    # The same keys while a button itself holds the focus (Tk buttons consume
    # some of them before the toplevel binding sees them).
    for btn in buttons:
        for seq, handler in (
            ("<Return>", lambda _e: activate()),
            ("<KP_Enter>", lambda _e: activate()),
            ("<space>", lambda _e: activate()),
            ("<Left>", lambda _e: move(-1)),
            ("<Up>", lambda _e: move(-1)),
            ("<Right>", lambda _e: move(1)),
            ("<Down>", lambda _e: move(1)),
        ):
            try:
                btn.bind(seq, handler)
            except Exception:
                pass

    render()
    return state


def _duplicate_dialog_ui(root, filename: str, existing_path: Path) -> tuple:
    """Build the "file already exists" question dialog.

    Returns ``(dialog, callback)`` where ``callback["value"]`` is set to
    ``"skip"`` or ``"replace"`` when a button is pressed (the dialog is
    destroyed with it). Closing the window counts as "skip".

    Answering by keyboard: the arrow keys select an option (Enter/Space
    presses it) and Ctrl+Y = Yes (replace) / Ctrl+N = No (skip) act
    immediately — as do the older Ctrl+S (replace) and Ctrl+Delete (skip).
    """
    dialog = ctk.CTkToplevel(root)
    dialog.title("File already exists")
    dialog.configure(fg_color=_BG)
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)

    # Center over the popup/screen, like every other FilePicker window.
    try:
        sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        dialog.geometry(f"520x266+{max((sw - 520) // 2, 0)}+{max((sh - 266) // 3, 0)}")
    except tk.TclError:
        pass

    callback = {"value": None}

    # Alt-block the dialog too: the popup released itself (and its hook)
    # before the duplicate question appears, so without this the gap would
    # let every other program react to Alt+<key> again while this dialog is
    # open. Ref-counted with the popup's hook (see altblock.remove(hwnd)).
    _alt_active = False
    try:
        import altblock as _altblock
        _alt_active = _altblock.install(dialog.winfo_id())
    except Exception:
        _alt_active = False

    def cleanup() -> None:
        nonlocal _alt_active
        if _alt_active:
            try:
                import altblock as _altblock
                _altblock.remove(dialog.winfo_id())
            except Exception:
                pass
            _alt_active = False

    def choose(choice: str) -> None:
        cleanup()
        callback["value"] = choice
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    ctk.CTkLabel(
        dialog, text="⚠ This filename already exists in the sorted folders",
        font=ctk.CTkFont(size=15, weight="bold"), text_color=_TEXT,
    ).pack(anchor="w", padx=18, pady=(18, 6))
    ctk.CTkLabel(
        dialog,
        text=f"\"{filename}\" already exists at:\n{existing_path.parent}\n\n"
             "Replace the old file with this new download?\n\n"
             "Ctrl+Y = Yes (replace)  •  Ctrl+N = No (skip)\n"
             "Arrows + Enter also work  •  Ctrl+S = replace  •  Ctrl+Delete = skip",
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED, justify="left",
        wraplength=480,
    ).pack(anchor="w", padx=18, pady=(0, 12))

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=18, pady=(0, 16))
    skip_btn = ctk.CTkButton(
        btn_row, text="No — Skip New File  (N)", command=lambda: choose("skip"),
        fg_color=_BG_FIELD, hover_color="#33334a", height=38,
        font=ctk.CTkFont(size=13), text_color=_TEXT,
    )
    skip_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
    replace_btn = ctk.CTkButton(
        btn_row, text="Yes — Replace Old with New  (Y)",
        command=lambda: choose("replace"),
        fg_color=_ACCENT, hover_color=_ACCENT_HOVER, height=38,
        font=ctk.CTkFont(size=13, weight="bold"), text_color="#ffffff",
    )
    replace_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))

    # Safe default: closing the dialog (or pressing Enter) keeps the old file.
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("skip"))
    # Ctrl shortcuts keep working while this dialog is open (they were dead
    # from an older build because the dialog owned all keyboard input):
    # Ctrl+S = "save", i.e. Replace Old with New; Ctrl+Delete = Skip.
    dialog.bind("<Control-s>", lambda _e: choose("replace"))
    dialog.bind("<Control-Delete>", lambda _e: choose("skip"))
    dialog.bind("<Escape>", lambda _e: choose("skip"))
    # Arrow selection + the letters shown on the buttons (Y = replace, N =
    # skip); the safe option (skip) is selected first, so a stray Enter never
    # overwrites an existing file.
    _wire_dialog_choices(dialog, [skip_btn, replace_btn], ["n", "y"], default_index=0)
    # Give the question the keyboard the moment it appears (the popup was
    # dismissed, so Windows may have handed focus back to another program).
    try:
        winfocus.claim(dialog)
    except Exception:
        pass
    return dialog, callback


def ask_duplicate_action(root, filename: str, existing_path: Path) -> str:
    """Ask what to do when the output filename already exists in sorted.

    Blocks (modal) until the user answers. Returns ``"skip"`` — keep the old
    file (the caller deletes the new download from the watch folder) — or
    ``"replace"`` — overwrite the old file with the new one. Closing the
    dialog counts as ``"skip"`` (the safe default).
    """
    dialog, callback = _duplicate_dialog_ui(root, filename, existing_path)
    dialog.wait_window()
    return callback["value"] or "skip"


def _cross_client_dialog_ui(parent, client: str, site: str, conflicts) -> tuple:
    """Build the "this site belongs to another client" warning dialog.

    *conflicts* is the ``[(other_client, their_site), ...]`` list from
    :meth:`ConfigManager.find_similar_site_other_client`. Returns
    ``(dialog, callback)`` where ``callback["value"]`` becomes:

    - ``"continue"`` — save under *client* anyway (the warning only);
    - ``"transfer"`` — move EVERY site of the conflicting client(s) into
      *client* and then save;
    - ``"cancel"`` — do nothing; the popup stays open so the user can pick
      the other client themselves.

    The dialog itself never touches the config: the caller performs the move
    only after the user explicitly clicks the transfer button. Closing the
    window (or Escape) cancels — there is no automatic shift and no default
    that changes any data.
    """
    dialog = ctk.CTkToplevel(parent)
    dialog.title("Site belongs to another client")
    dialog.configure(fg_color=_BG)
    # Stay ABOVE the popup: without transient(), the two topmost windows
    # fight and this dialog could land behind the popup (the user then had to
    # click its taskbar entry to bring it up). transient() ties it to the
    # popup, and lift()/focus_force() raise it now. Only tie to a VISIBLE
    # parent: transient to a withdrawn window unmaps the dialog (Tk) — the
    # inline prompts and tests that pass an unmapped parent keep working.
    try:
        if parent is not None and parent.winfo_viewable():
            dialog.transient(parent)
    except Exception:
        pass
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    # Center over the POPUP (not the screen corner) so it is visibly attached
    # to the window that asked the question.
    try:
        overlay = parent is not None and parent.winfo_viewable() \
            and parent.winfo_width() > 100
    except Exception:
        overlay = False
    if overlay:
        try:
            parent.update_idletasks()
            w, h = 560, 330
            px = parent.winfo_rootx() + max((parent.winfo_width() - w) // 2, 0)
            py = parent.winfo_rooty() + max((parent.winfo_height() - h) // 2, 0)
            dialog.geometry(f"{w}x{h}+{max(px, 0)}+{max(py, 0)}")
        except tk.TclError:
            overlay = False
    if not overlay:
        try:
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            dialog.geometry(f"560x330+{max((sw - 560) // 2, 0)}+{max((sh - 330) // 3, 0)}")
        except tk.TclError:
            pass

    callback = {"value": None}

    def choose(choice: str) -> None:
        try:
            dialog.grab_release()
        except Exception:
            pass
        callback["value"] = choice
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    others = []
    for other, other_site in conflicts:
        if other not in others:
            others.append(other)
    lines = "\n".join(f"•  {other}  →  {other_site}"
                      for other, other_site in conflicts[:6])
    if len(conflicts) > 6:
        lines += f"\n•  … and {len(conflicts) - 6} more"
    if len(others) == 1:
        transfer_text = f"Move {others[0]}'s sites here"
    else:
        transfer_text = f"Move all {len(others)} clients' sites here"

    ctk.CTkLabel(
        dialog, text="⚠ This site is already used by another client",
        font=ctk.CTkFont(size=15, weight="bold"), text_color=_TEXT,
    ).pack(anchor="w", padx=18, pady=(18, 6))
    ctk.CTkLabel(
        dialog,
        text=f"\"{site}\" is the same place as a site of another client:\n"
             f"{lines}\n\n"
             f"Saving keeps this file under \"{client}\". Nothing is moved\n"
             f"automatically — choose what to do:\n\n"
             f"Ctrl+Y = keep  •  Ctrl+M = move sites  •  Ctrl+N = cancel\n"
             f"Arrows + Enter also work",
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED, justify="left",
        wraplength=520,
    ).pack(anchor="w", padx=18, pady=(0, 12))

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=18, pady=(0, 16))
    keep_btn = ctk.CTkButton(
        btn_row, text=f"Keep under {client}  (Y)", command=lambda: choose("continue"),
        fg_color=_ACCENT, hover_color=_ACCENT_HOVER, height=38,
        font=ctk.CTkFont(size=12, weight="bold"), text_color="#ffffff",
    )
    keep_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
    move_btn = ctk.CTkButton(
        btn_row, text=f"{transfer_text}  (M)", command=lambda: choose("transfer"),
        fg_color=_BG_FIELD, hover_color="#33334a", height=38,
        font=ctk.CTkFont(size=12), text_color=_TEXT,
    )
    move_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
    cancel_btn = ctk.CTkButton(
        btn_row, text="Cancel  (N)", command=lambda: choose("cancel"),
        fg_color=_BG_FIELD, hover_color="#33334a", width=90, height=38,
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
    )
    cancel_btn.pack(side="left", fill="x")

    # Cancel is the safe default: closing or Escape changes nothing, leaving
    # the popup open so the user can switch the client themselves.
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
    dialog.bind("<Escape>", lambda _e: choose("cancel"))
    # Arrow selection + the letters shown on the buttons: Y = keep this file
    # here, M = move the other client's sites, N = cancel.
    _wire_dialog_choices(
        dialog, [keep_btn, move_btn, cancel_btn], ["y", "m", "n"], default_index=0)

    # Re-raise once mapped: topmost + transient can lose the race against the
    # popup's own topmost flag on the first map, which put this dialog behind
    # the popup. The modal grab is taken here too (the window is viewable by
    # now; grabbing an unmapped window fails).
    def _raise_me() -> None:
        try:
            dialog.lift()
            dialog.attributes("-topmost", True)
            dialog.focus_force()
        except Exception:
            pass
        try:
            if dialog.winfo_viewable() and dialog.grab_current() is None:
                dialog.grab_set()
        except Exception:
            pass

    try:
        dialog.after(0, _raise_me)
    except Exception:
        pass
    # ...and take the Windows foreground too (the popup may be the foreground
    # window of another process' input queue, which swallows focus_force).
    try:
        winfocus.claim(dialog)
    except Exception:
        pass
    return dialog, callback


def ask_cross_client_site(parent, client: str, site: str, conflicts) -> str:
    """Blocking wrapper around :func:`_cross_client_dialog_ui`.

    Returns ``"continue"``, ``"transfer"`` or ``"cancel"`` (closing the
    dialog counts as ``"cancel"``).
    """
    dialog, callback = _cross_client_dialog_ui(parent, client, site, conflicts)
    dialog.wait_window()
    return callback["value"] or "cancel"

# File types the preview viewer can render (see viewer.py).
_SUPPORTED_PREVIEW_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".pdf", ".xlsx", ".xlsm", ".xls",
}

# Dark theme colours — muted background, high-contrast buttons/text.
_BG = "#15151d"
_BG_SECONDARY = "#1f1f2b"
_BG_FIELD = "#262633"
_ACCENT = "#5b8cff"
_ACCENT_HOVER = "#3f6fe0"
_TEXT = "#f2f2f7"
_TEXT_MUTED = "#b6b6c9"
_SUCCESS = "#7ad17a"
_DANGER = "#ff6b6b"


class SearchableDropdown(ctk.CTkFrame):
    """A searchable combo: a text entry that filters a dropdown of choices.

    Uses a Listbox in an overrideredirect Toplevel so the dropdown never
    steals keyboard focus — the user keeps typing while the list updates live.
    Shows at most 5 matches live as you type (no Enter needed).
    Replaces the plain CTkOptionMenu with the same interface the popup expects:
    ``get()``, ``configure(values=...)``, ``set(value)`` and an ``on_change``
    callback.
    """

    def __init__(self, parent, values, on_change, width=None) -> None:
        super().__init__(parent, fg_color="transparent")
        self._values = list(values)
        self._on_change = on_change
        self._value = ""
        self._dropdown_open = False
        self._highlight_index = -1

        self.entry = ctk.CTkEntry(
            self, fg_color=_BG_FIELD, border_color=_BG_FIELD,
            text_color=_TEXT, placeholder_text="Search or select…",
        )
        if width:
            self.entry.configure(width=width)
        self.entry.pack(fill="x")
        # Pack this frame itself into its parent (the popup form).
        self.pack(fill="x", pady=(0, 6))

        # Dropdown window (overrideredirect Toplevel with a Listbox)
        # Created lazily on first open so winfo_toplevel() is valid.
        self._top = None
        self._listbox = None

        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Escape>", lambda _e: self._close())
        self.entry.bind("<Down>", lambda _e: self._move(1))
        self.entry.bind("<Up>", lambda _e: self._move(-1))
        self.entry.bind("<FocusIn>", lambda _e: self._show_all())
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<Button-1>", lambda _e: self.after(10, self._show_all))

        # Keep dropdown positioned when popup moves
        self.bind("<Configure>", lambda _e: self._reposition() if self._dropdown_open else None)

    def _ensure_dropdown(self) -> None:
        if self._top is not None and self._top.winfo_exists():
            return
        top = tk.Toplevel(self.winfo_toplevel())
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=_BG_FIELD)
        lb = tk.Listbox(
            top, bg=_BG_FIELD, fg=_TEXT,
            selectbackground=_ACCENT, selectforeground="#ffffff",
            activestyle="none", highlightthickness=0, bd=0,
            font=tkfont.Font(family="Segoe UI", size=11),
            exportselection=False,
        )
        lb.pack(fill="both", expand=True, padx=1, pady=1)
        lb.bind("<ButtonRelease-1>", self._on_list_click)
        self._top = top
        self._listbox = lb

    # ------------------------------------------------------------------
    def get(self) -> str:
        """The currently displayed value: the selected match if set, otherwise
        whatever the user typed (so typing + Save without clicking still uses
        the typed value). Resolves typed text to the canonical case when it
        matches an existing value case-insensitively."""
        typed = self.entry.get().strip()
        if typed:
            for v in self._values:
                if v.lower() == typed.lower():
                    return v
            return typed
        return self._value

    def set(self, value: str) -> None:
        self._value = value
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def configure(self, **kwargs) -> None:
        if "values" in kwargs:
            self._values = list(kwargs.pop("values"))
            if self._value not in self._values:
                self._value = ""
            self.entry.delete(0, "end")
            if self._value:
                self.entry.insert(0, self._value)
            self._close()
        for key, val in kwargs.items():
            try:
                getattr(self.entry, "configure")(**{key: val})
            except Exception:
                pass
        return self

    def update_values_preserve_typed(self, new_values) -> None:
        """Update dropdown values without losing what the user is currently typing.

        Unlike ``configure(values=...)`` which clears the entry when the selected
        value is no longer valid, this keeps the raw entry text (a partial filter
        like ``\"LODH\"``) and only resolves to canonical case when the typed text
        exactly matches a new value.
        """
        typed = self.entry.get()
        typed_stripped = typed.strip()
        old_value = self._value
        self._values = list(new_values)

        # Decide what to show in the entry:
        # - If user is actively typing (entry != old canonical value), keep typed text
        #   (unless typed exactly matches a new canonical value, then canonicalize).
        # - Otherwise (entry == canonical or empty), show canonical if still valid.
        sentinels = {ADD_NEW_CLIENT_OPTION, ADD_NEW_SITE_OPTION,
                     ADD_NEW_COMPANY_OPTION, ADD_NEW_MATERIAL_OPTION}
        # Check for exact case-insensitive match to a new value
        exact_match = None
        if typed_stripped:
            for v in self._values:
                if v in sentinels:
                    continue
                if v.lower() == typed_stripped.lower():
                    exact_match = v
                    break

        typing = typed != old_value
        if exact_match is not None:
            # Typed is an exact (case-insensitive) match to a new value — canonicalize
            self._value = exact_match
            self.entry.delete(0, "end")
            self.entry.insert(0, exact_match)
        elif typing:
            # User is actively typing a partial filter (or cleared to empty) — preserve typed
            if old_value not in self._values:
                self._value = ""
            # else keep old_value as-is for get() fallback when entry empty
            self.entry.delete(0, "end")
            self.entry.insert(0, typed)
        else:
            # Not typing (entry == canonical) — show canonical if still valid
            if self._value not in self._values:
                self._value = ""
            self.entry.delete(0, "end")
            if self._value:
                self.entry.insert(0, self._value)
            elif typed and typed_stripped in sentinels:
                self.entry.insert(0, typed)
        self._close()
        # If dropdown was open, refresh its listbox to reflect new values
        if self._dropdown_open:
            try:
                self._update_listbox()
                self._reposition()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _visible_items(self) -> list:
        text = self.entry.get().strip().lower()
        if not text:
            matches = list(self._values)
        else:
            matches = [v for v in self._values if text in v.lower()]
        return matches[:_MAX_DROPDOWN_RESULTS]

    def _show_all(self) -> None:
        self._ensure_dropdown()
        self._update_listbox()
        self._open()

    def _open(self) -> None:
        self._ensure_dropdown()
        if self._dropdown_open:
            self._update_listbox()
            self._reposition()
            return
        self._update_listbox()
        if self._listbox.size() == 0 or self._listbox.get(0) == "(no matches)":
            return
        self._reposition()
        try:
            self._top.deiconify()
            self._top.lift()
            self._dropdown_open = True
            self.entry.focus_set()
        except tk.TclError:
            self._dropdown_open = False

    def _reposition(self) -> None:
        try:
            x = self.entry.winfo_rootx()
            y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
            w = self.entry.winfo_width()
            h = self._listbox.size() * 22 + 4
            h = min(h, _MAX_DROPDOWN_RESULTS * 22 + 4)
            if h < 22:
                h = 22
            self._top.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._dropdown_open:
            try:
                self._top.withdraw()
            except tk.TclError:
                pass
            self._dropdown_open = False
            self._highlight_index = -1

    def _on_focus_out(self, _e=None) -> None:
        self.after(180, self._close_if_not_focused)

    def _close_if_not_focused(self) -> None:
        try:
            if self._top is None or not self._top.winfo_exists():
                self._close()
                return
            focused = self.focus_displayof()
            if focused is not None and str(focused).startswith(str(self._top)):
                return
        except tk.TclError:
            pass
        self._close()

    def _update_listbox(self) -> None:
        self._ensure_dropdown()
        text = self.entry.get().strip().lower()
        if not text:
            matches = list(self._values)
        else:
            matches = [v for v in self._values if text in v.lower()]
        self._listbox.delete(0, "end")
        self._highlight_index = -1
        if not matches:
            self._listbox.insert("end", "(no matches)")
            self._listbox.itemconfig(0, fg=_TEXT_MUTED)
            return
        for item in matches[:_MAX_DROPDOWN_RESULTS]:
            self._listbox.insert("end", item)
        if len(matches) > _MAX_DROPDOWN_RESULTS:
            self._listbox.insert("end", f"… {len(matches) - _MAX_DROPDOWN_RESULTS} more (keep typing)")
            self._listbox.itemconfig("end", fg=_TEXT_MUTED)
        if self._listbox.size() > 0:
            self._highlight_index = 0
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(0)
            self._listbox.activate(0)

    def _choose(self, value: str) -> None:
        if value.startswith("… ") or value == "(no matches)":
            return
        self._close()
        self._value = value
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        self.entry.focus_set()
        if self._on_change:
            self._on_change(value)

    def _on_list_click(self, event) -> None:
        idx = self._listbox.nearest(event.y)
        if 0 <= idx < self._listbox.size():
            val = self._listbox.get(idx)
            self._choose(val)

    def _move(self, delta: int) -> str:
        if not self._dropdown_open:
            self._show_all()
            return "break"
        n = self._listbox.size()
        if n == 0:
            return "break"
        self._highlight_index = (self._highlight_index + delta) % n
        val = self._listbox.get(self._highlight_index)
        if val.startswith("… ") or val == "(no matches)":
            self._highlight_index = (self._highlight_index + delta) % n
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(self._highlight_index)
        self._listbox.activate(self._highlight_index)
        self._listbox.see(self._highlight_index)
        return "break"

    def _on_return(self, _e=None) -> str:
        # Enter always closes the dropdown. If a valid item is highlighted,
        # choose it (calls on_change); otherwise keep the typed value.
        if self._dropdown_open and self._listbox.size() > 0:
            idx = self._highlight_index if 0 <= self._highlight_index < self._listbox.size() else 0
            val = self._listbox.get(idx)
            if not val.startswith("… ") and val != "(no matches)":
                self._choose(val)
                return "break"
        # No valid highlight (typed a new value or "(no matches)") — just close
        self._close()
        return "break"

    def _on_key(self, _e=None) -> None:
        # Don't reopen immediately after Enter (Return) — _on_return already
        # closed — and never re-filter/reset after Up/Down: the arrow keys
        # are handled by _move() and re-running _update_listbox() here would
        # snap the highlight BACK to the top item ("arrow keys always reset
        # back to the first match").
        if _e is not None and getattr(_e, "keysym", "") in ("Return", "Up", "Down"):
            return
        self._update_listbox()
        if self._listbox.size() > 0 and self._listbox.get(0) != "(no matches)":
            self._reposition()
            if not self._dropdown_open:
                try:
                    self._ensure_dropdown()
                    self._top.deiconify()
                    self._top.lift()
                    self._dropdown_open = True
                except tk.TclError:
                    pass
            self.entry.focus_set()
        else:
            self._close()

    def _on_focus(self, _e=None) -> None:
        self._update_listbox()
        if self._listbox.size() > 0 and self._listbox.get(0) != "(no matches)":
            self._open()

    def _key_pressed(self, _e=None) -> None:
        self._on_key()


class MappingDialog:
    """Modal "Map Client / Map Site" editor (opened by the 🗺 Map buttons).

    Maps the name the current document shows (the alias SOURCE) to the
    catalog name to use instead (the TARGET): from then on, whenever OCR
    reads the source name — or a popup is saved with it — the target is
    used instead. Shows the existing mappings in a searchable list and lets
    the user add / update / remove entries, using the same search-as-you-
    type dropdowns the Client/Site fields use.
    """

    def __init__(self, parent, kind: str, config, names: List[str],
                 current: str = "",
                 on_changed: Optional[Callable[[], None]] = None) -> None:
        self.kind = kind  # "client" | "site" | "material"
        self.config = config
        self._on_changed = on_changed
        self._rows: List[tuple] = []  # [(source, target), ...] in the list

        if kind == "material":
            title, what = "Map Material", "material"
        elif kind == "client":
            title, what = "Map Client", "client"
        else:
            title, what = "Map Site", "site"

        self.win = ctk.CTkToplevel(parent)
        self.win.title(title)
        self.win.configure(fg_color=_BG)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        try:
            self.win.transient(parent)
        except Exception:
            pass
        try:
            sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            w, h = 560, 640
            self.win.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 3, 0)}")
        except tk.TclError:
            pass
        self.win.lift()

        body = ctk.CTkFrame(self.win, fg_color=_BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        if kind == "material":
            explain = ("When OCR reads the \"Description of Goods\", the "
                       "word on the left is treated as the material on the "
                       "right: future downloads mentioning \"nuts\" or "
                       "\"bolts\" pre-select \"Screw\" automatically, even "
                       "though the document never writes the material name.")
        else:
            explain = (f"When OCR (or this popup) reads the name on the left, "
                       f"FilePicker switches it to the name on the right. Future "
                       f"downloads of the same {what} are filed under the mapped "
                       f"name automatically.")
        ctk.CTkLabel(
            body,
            text=explain,
            font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED, justify="left",
            wraplength=520,
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(
            body,
            text="Goods description shows:"
                 if kind == "material" else "Document shows / OCR reads:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=_TEXT_MUTED,
        ).pack(anchor="w", pady=(0, 2))
        self.source_dd = SearchableDropdown(body, values=list(names),
                                            on_change=None)
        self.source_dd.entry.configure(
            placeholder_text=("Search or type the word the document has…"
                              if kind == "material"
                              else "Search or type the name the document has…"),
        )
        self.source_dd.set(current)

        ctk.CTkLabel(
            body,
            text="Select as material:"
                 if kind == "material" else "Map to:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=_TEXT_MUTED,
        ).pack(anchor="w", pady=(6, 2))
        self.target_dd = SearchableDropdown(body, values=list(names),
                                            on_change=None)
        self.target_dd.entry.configure(
            placeholder_text="Search or type the name to use instead…",
        )

        self._error_label = ctk.CTkLabel(body, text="",
                                         font=ctk.CTkFont(size=11),
                                         text_color=_DANGER, anchor="w")
        self._error_label.pack(fill="x", pady=(2, 0))

        ctk.CTkButton(
            body, text="＋ Add / Update Mapping", command=self._apply_map,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, height=34,
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#ffffff",
        ).pack(fill="x", pady=(4, 10))

        ctk.CTkLabel(body, text="Existing mappings:",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self._filter_entry = ctk.CTkEntry(
            body, fg_color=_BG_FIELD, border_color=_BG_FIELD, text_color=_TEXT,
            placeholder_text="Search mappings…",
        )
        self._filter_entry.pack(fill="x", pady=(0, 4))
        self._filter_entry.bind("<KeyRelease>", lambda _e: self._refresh_list())

        self._listbox = tk.Listbox(
            body, bg=_BG_FIELD, fg=_TEXT, selectbackground=_ACCENT,
            selectforeground="#ffffff", activestyle="none", highlightthickness=0,
            bd=0, font=tkfont.Font(family="Segoe UI", size=11),
            exportselection=False, height=8,
        )
        self._listbox.pack(fill="both", expand=True)

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(
            btn_row, text="🗑 Remove Selected", command=self._remove_selected,
            fg_color="#3a2b2b", hover_color="#4a3535", height=32,
            font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(
            btn_row, text="Close", command=self._close,
            fg_color=_BG_FIELD, hover_color="#33334a", height=32,
            font=ctk.CTkFont(size=12), text_color=_TEXT,
        ).pack(side="left", expand=True, fill="x")

        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_list()
        try:
            self.source_dd.entry.focus_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    def _alias_map(self) -> Dict[str, str]:
        if self.kind == "client":
            return self.config.client_aliases
        if self.kind == "material":
            return self.config.material_aliases
        return self.config.site_aliases

    def _set_alias(self, source: str, target: str) -> bool:
        if self.kind == "client":
            return self.config.set_client_alias(source, target)
        if self.kind == "material":
            return self.config.set_material_alias(source, target)
        return self.config.set_site_alias(source, target)

    def _remove_alias(self, source: str) -> bool:
        if self.kind == "client":
            return self.config.remove_client_alias(source)
        if self.kind == "material":
            return self.config.remove_material_alias(source)
        return self.config.remove_site_alias(source)

    def _apply_map(self) -> None:
        source = self.source_dd.get().strip()
        target = self.target_dd.get().strip()
        if not source or not target:
            self._error_label.configure(text="⚠ Both fields are required.")
            return
        if source.lower() == target.lower():
            self._error_label.configure(
                text="⚠ Source and target are the same name.")
            return
        changed = self._set_alias(source, target)
        self._error_label.configure(
            text="✓ Mapping saved — applied from the next OCR read."
                 if changed else "✓ Mapping already set.")
        self._refresh_list()
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                pass

    def _refresh_list(self) -> None:
        rows = [(str(k), str(v)) for k, v in self._alias_map().items()]
        text = self._filter_entry.get().strip().lower()
        if text:
            rows = [r for r in rows if text in f"{r[0]} {r[1]}".lower()]
        rows.sort(key=lambda r: r[0].lower())
        self._rows = rows
        self._listbox.delete(0, "end")
        if not rows:
            self._listbox.insert("end", "(no mappings)")
            try:
                self._listbox.itemconfig(0, fg=_TEXT_MUTED)
            except tk.TclError:
                pass
            return
        for src, tgt in rows:
            self._listbox.insert("end", f"{src}  →  {tgt}")

    def _remove_selected(self) -> None:
        sel = self._listbox.curselection()
        if not sel or not self._rows:
            return
        source = self._rows[sel[0]][0]
        self._remove_alias(source)
        self._refresh_list()
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                pass

    def _close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# Regional spellings / known typos treated as the same WORD when matching a
# material NAME against the "Description of Goods" text (the OCR transcription
# keeps the document's spelling: "galvanised", "aluminum", "fastener").
_MATERIAL_WORD_ALIASES = {
    "galvanized": ("galvanised",),
    "aluminium": ("aluminum",),
    "fastner": ("fastener",),
}

# Separators between "Description of Goods" item headings. The OCR prompt asks
# for comma-separated headings, but models answer with one per line, with
# semicolons, table pipes or bullets just as often — every one of those starts
# a NEW item, and each item gets at most ONE material (see
# FilePickerPopup._goods_material_matches).
_GOODS_ITEM_SPLIT = re.compile(r"[,;|\n\r•·]+")


class FilePickerPopup:
    """Modal dialog that gathers metadata and hands it to a callback."""

    def __init__(
        self,
        config: ConfigManager,
        file_path: Path,
        on_submit: Callable[[dict], None],
        on_skip: Callable[[], None],
        ocr_pool=None,
        on_skip_all: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.file_path = Path(file_path)
        self.on_submit = on_submit
        self.on_skip = on_skip
        self.ocr_pool = ocr_pool  # OcrPool (eager background OCR) or None
        # "Skip All & Delete": called (after this popup released itself) so
        # the controller can drop every queued popup and remove the files
        # from the watch folder. None hides the button.
        self.on_skip_all = on_skip_all

        # Internal UI state.
        self._company_var = tk.StringVar()
        self._client_var = tk.StringVar()
        self._doc_type_var = tk.StringVar(value="DC")
        self._serial_var = tk.StringVar()
        # Received Copy is UNCHECKED by default (unchecked = Submitted).
        self._received_var = tk.BooleanVar(value=False)
        self._selected_materials: List[str] = []
        # OCR PROVENANCE. What the last OCR application put in each field, and
        # which materials it selected, so a later read (the ↻ Retry button) may
        # replace OCR's own guesses — while anything the user typed or toggled
        # is never touched. Without this a retry could not correct a wrong
        # read at all: every field was already non-empty, so the "never
        # clobber" rule (right for the user's edits, wrong for our own earlier
        # fill) rejected the new answer and the popup kept the wrong values.
        self._ocr_filled: Dict[str, str] = {}
        self._ocr_materials: set = set()
        # Materials the user toggled OFF by hand: OCR must not put them back.
        self._user_off_materials: set = set()
        # True while a "↻ Retry OCR" read is in flight (its outcome may clear
        # the values the rejected read had filled).
        self._ocr_retrying = False
        # Names of the fields a read left alone because the user had typed
        # them (used for the status line: "kept your site").
        self._ocr_kept_user_edits: List[str] = []
        # Company name placed by OCR that is NOT in the catalog — kept across
        # live-config refreshes until the user picks a menu value.
        self._ocr_company_override: Optional[str] = None
        # Raw Company/Client/Site values the OCR returned (before any
        # canonicalization) — shown highlighted in yellow in the preview.
        self._ocr_highlight_terms: List[str] = []
        # Live OCR status line: `_ocr_progress_after` is the pending
        # after()-tick that refreshes the "reading document…" counters, and
        # `_ocr_poll_done` stops it once the outcome has been applied.
        self._ocr_progress_after = None
        self._ocr_poll_done = False

        # Material name -> shortcode mapping loaded once.
        self._materials_map: Dict[str, str] = {}

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._build_window()
        self._build_ui()
        self._reload_config_state()
        self._set_banner()

        # Preview open by default: "preview_open_by_default": true (default)
        # opens the preview with the popup; false starts with the form only
        # (Ctrl+P / the Preview button still toggles it).
        if self.config.preview_open_by_default \
                and self.file_path.suffix.lower() in _SUPPORTED_PREVIEW_EXTS:
            self._toggle_preview()

        # Live-update the preview when the serial or checkbox changes.
        self._serial_var.trace_add("write", lambda *_: self._refresh_preview())
        self._received_var.trace_add("write", lambda *_: self._refresh_preview())

        # Keep the popup on top of everything AND give it the keyboard: the
        # download finished while the user was in another program, so Windows
        # refuses a plain focus_force() from this background process — the
        # popup was visible but every keystroke still went elsewhere (Ctrl+S
        # did nothing until the popup was clicked). winfocus takes the
        # foreground properly and retries right after the window is mapped.
        try:
            winfocus.claim(self.window)
        except Exception as exc:
            print(f"[filepicker] focus claim error: {exc}")
            try:
                self.window.attributes("-topmost", True)
                self.window.lift()
                self.window.focus_force()
            except Exception:
                pass
        # A popup can be de-minimised/restored from the taskbar: take the
        # keyboard again whenever it is mapped, not only at creation.
        try:
            self.window.bind("<Map>", lambda _e: winfocus.claim(self.window), add="+")
        except Exception:
            pass
        # Watchdog: CustomTkinter briefly withdraws every new window to colour
        # its title bar, and if that dance ever ends with the window still
        # hidden the popup would sit there invisible while the controller
        # waits for it — the user would see downloads being OCR'd and no popup
        # at all, forever. Make sure it is really on screen and repair it if
        # not (never for an intentionally minimised popup).
        self._visible_checks_left = 3
        try:
            self.window.after(_POPUP_VISIBLE_CHECK_MS, self._ensure_popup_visible)
        except Exception:
            pass

        # Global Alt suppression (Windows): while this popup is open, Alt+key
        # is swallowed for every other program (AutoDesk etc.) and re-posted
        # only to this window, so the material hotkeys keep working and no
        # other app ever reacts. Config: "block_alt_for_other_apps": true
        # (default). No-op off Windows.
        self._alt_block_active = False
        if self.config.block_alt_for_other_apps:
            try:
                import altblock
                self._alt_block_active = altblock.install(self.window.winfo_id())
            except Exception as exc:
                print(f"[filepicker] alt-block hook error: {exc}")
                self._alt_block_active = False

        # Live config: refresh while open if someone pushes a new clients/sites list.
        self._config_poll_after = None
        self._start_config_poll()

        # OCR auto-fill status (shows the disabled/no-key reason explicitly —
        # never silently nothing).
        self._start_ocr()

    def _set_banner(self) -> None:
        try:
            size = self.file_path.stat().st_size
            human = self._human_size(size)
        except OSError:
            human = "unknown size"
        self._banner_name.configure(text=self.file_path.name)
        self._banner_size.configure(text=f"{human}  •  {self.file_path}")

        # Disable the preview button for file types the viewer can't render.
        if self.file_path.suffix.lower() not in _SUPPORTED_PREVIEW_EXTS:
            self.preview_btn.configure(state="disabled")

    def _toggle_preview(self) -> None:
        """Open/close the file preview embedded on the right of the popup."""
        from viewer import PreviewWindow

        if self._preview is not None:
            # --- Close the preview: collapse back to the form only. ---
            try:
                self._preview.destroy()
            except Exception:
                pass
            self._preview = None
            try:
                self.preview_pane.pack_forget()
            except tk.TclError:
                pass
            self._center_window(560)
            self.preview_btn.configure(text="👁 Preview")
            return

        # --- Open the preview: embed it and expand the window right. ---
        # An older build's preview close destroyed the container pane itself,
        # which made the next "Preview" click fail with a dead widget. Recreate
        # the pane if it is gone so open -> close -> open always works.
        try:
            if not self.preview_pane.winfo_exists():
                self._rebuild_preview_pane()
        except tk.TclError:
            self._rebuild_preview_pane()
        try:
            self.preview_pane.pack(side="left", fill="both", expand=True)
        except tk.TclError:
            self._rebuild_preview_pane()
            self.preview_pane.pack(side="left", fill="both", expand=True)
        try:
            self._preview = PreviewWindow(
                self.window, self.file_path, container=self.preview_pane
            )
        except Exception as exc:
            self._preview = None
            try:
                self.preview_pane.pack_forget()
            except tk.TclError:
                pass
            self._center_window(560)
            print(f"[filepicker] preview error: {exc}")
            return
        # Yellow-highlight the values OCR found in the PDF page.
        try:
            self._preview.set_highlight_terms(
                getattr(self, "_ocr_highlight_terms", [])
            )
        except Exception:
            pass
        self._center_window(1180)
        self.preview_btn.configure(text="✕ Close Preview")

    def _center_window(self, width: int) -> None:
        """Resize to ``width`` and re-center the popup horizontally.

        The popup opens centered at 560px; opening the preview widens it to
        1180px and would otherwise just grow to the right, off-centre. Re-
        centering on both open and close keeps the window's centre on the
        screen centre at either size. The vertical position is preserved.
        """
        try:
            x = max((self.window.winfo_screenwidth() - width) // 2, 0)
            y = self.window.winfo_y()
            self.window.geometry(f"{width}x{self._win_h}+{x}+{y}")
        except tk.TclError:
            pass

    def _rebuild_preview_pane(self) -> None:
        """(Re)create the embedded preview pane inside the popup's body.

        Normally the pane is built once in ``_build_ui`` and reused for every
        preview open/close cycle. It is only ever gone if a preview close
        (from an older build) destroyed the frame itself, so this rebuilds it
        to recover without restarting the popup.
        """
        self.preview_pane = ctk.CTkFrame(self._body, fg_color=_BG)
        self.preview_pane.pack(side="left", fill="both", expand=True)
        self.preview_pane.pack_forget()

    @staticmethod
    def _human_size(num: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(num) < 1024.0:
                return f"{num:.1f} {unit}"
            num /= 1024.0
        return f"{num:.1f} PB"

    # ------------------------------------------------------------------
    # Compact-layout helpers (short screens / scaled-up displays)
    # ------------------------------------------------------------------
    def _sp(self, value: int, minimum: int = 1) -> int:
        """A vertical spacing value, halved in the compact layout.

        Zero stays zero (a pack with no padding must not invent one), and the
        result never drops below *minimum* so a gap is still a gap.
        """
        value = int(value or 0)
        if value <= 0:
            return 0
        if not self._compact:
            return value
        return max(int(minimum), int(round(value / 2)))

    def _h(self, normal: int, compact: int) -> int:
        """A widget height: *compact* on short screens, *normal* otherwise."""
        return compact if self._compact else normal

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------
    def _build_window(self) -> None:
        self.window = ctk.CTkToplevel()
        self.window.title(f"FilePicker v{VERSION} — New Download")
        # Open pinned to the top of the screen (title bar touches the top
        # edge) and horizontally centered, so it never needs to be dragged up.
        # The height is clamped to the screen so the bottom controls (Save /
        # Skip) are never cut off on shorter displays (e.g. 1366x768 laptops).
        # When "preview_open_by_default" is true (default) the window starts
        # at the wide, preview-open size so it never visibly jumps.
        _screen_w = self.window.winfo_screenwidth()
        _screen_h = self.window.winfo_screenheight()
        # Height: prefer 820, never taller than the screen. On short screens
        # the window shrinks below the old 700px floor — the layout adapts
        # (the bottom bar is pinned and the material chip panel gives up
        # rows), so the filename preview and the Save/Skip buttons are never
        # pushed off the bottom.
        self._win_h = max(min(820, _screen_h - 20), 560)
        # Compact spacing: on short screens (and on displays where the DPI
        # scaling makes every widget bigger) the default spacing does not fit,
        # and Tk squeezes the LAST packed widgets — which used to be the
        # Received Copy checkbox (a 2px sliver at 1366x768) and, on even
        # shorter screens, the Serial number field. Scale the paddings/heights
        # down so the whole form stays visible.
        try:
            _scale = float(ctk.ScalingTracker.get_widget_scaling(self.window))
        except Exception:
            _scale = 1.0
        if not _scale or _scale <= 0:
            _scale = 1.0
        self._compact = self._win_h < _FORM_H_COMFORT * _scale
        # Row pitch of the material chip panel (see _MATERIAL_ROW_PITCH*).
        self._row_pitch = (
            _MATERIAL_ROW_PITCH_COMPACT if self._compact else _MATERIAL_ROW_PITCH
        )
        _width = 560
        if self.config.preview_open_by_default \
                and self.file_path.suffix.lower() in _SUPPORTED_PREVIEW_EXTS:
            _width = 1180
        self.window.geometry(f"{_width}x{self._win_h}+{max((_screen_w - _width) // 2, 0)}+0")
        self.window.configure(fg_color=_BG)
        self.window.resizable(True, True)  # height adjustable
        self.window.minsize(560, 560)
        self.window.protocol("WM_DELETE_WINDOW", self._skip)

        # Deliberately NOT transient() (a childless transient window has no
        # taskbar entry on Windows — nothing to restore the popup from after
        # minimizing) and deliberately NO grab_set(): a grabbed Tk window on
        # Windows confines the pointer, so the native title-bar minimize
        # button becomes unreachable/ignored. Without the grab the popup is
        # still topmost and focused; the user can minimize it from the title
        # bar (or the in-banner "—" button) and restore it from the taskbar.

        # Material hotkeys: hold Alt and tap a material's 2-letter code
        # (e.g. Alt+AL = Aluminium, Alt+SS = Stainless Steel) to toggle it.
        # Bound on the window so it works from ANY field in the popup —
        # not just the materials area. Alt+RC toggles the Received checkbox.
        self._material_by_code: Dict[str, str] = {}
        self._alt_seq = ""
        self._alt_seq_after = None
        self.window.bind("<Alt-KeyPress>", self._on_alt_key)
        self.window.bind("<Alt-KeyRelease>", self._on_alt_release)
        self.window.bind("<FocusOut>", lambda _e: self._reset_alt_seq())

        # RIGHT Alt chords: Tk on Windows does not reliably set its Alt
        # modifier for the right Alt key, so <Alt-KeyPress> never fires for
        # it. Track the physical Alt_R key and route its letters through a
        # plain <KeyPress> binding instead. Left-Alt letters keep the
        # modifier path; a Mod1 event seen here means <Alt-KeyPress> already
        # handled it, so nothing is ever toggled twice.
        self._right_alt_down = False
        self.window.bind("<KeyPress>", self._on_any_key)
        self.window.bind("<KeyRelease>", self._on_any_key_release)

        # Ctrl shortcuts (work from any field): Ctrl+S = Save & Organize,
        # Ctrl+Delete = Skip / Keep Original, Ctrl+P = open/close Preview.
        self.window.bind("<Control-s>", lambda _e: self._submit())
        self.window.bind("<Control-Delete>", lambda _e: self._skip())
        self.window.bind("<Control-p>", lambda _e: self._toggle_preview())

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        container = ctk.CTkFrame(self.window, fg_color=_BG, corner_radius=0)
        container.pack(fill="both", expand=True, padx=14, pady=10)

        # Horizontal body: the metadata form on the left, and a preview pane
        # on the right that the window expands into when Preview is opened.
        body = ctk.CTkFrame(container, fg_color=_BG)
        body.pack(fill="both", expand=True)
        self._body = body  # keeps the preview pane rebuildable (see _rebuild_preview_pane)

        self.form_frame = ctk.CTkFrame(body, fg_color=_BG, width=524)
        self.form_frame.pack(side="left", fill="y")
        self.form_frame.pack_propagate(False)

        self.preview_pane = ctk.CTkFrame(body, fg_color=_BG)
        self.preview_pane.pack(side="left", fill="both", expand=True)
        self.preview_pane.pack_forget()  # hidden until the user opens preview
        self._preview = None

        f = self.form_frame

        # -- Bottom bar (PINNED) ----------------------------------------
        # Save/Skip and the live filename preview are packed FIRST with
        # side="bottom" so Tk always reserves their space at the bottom of
        # the popup. On short screens the middle of the form (the material
        # chip panel) shrinks/scrolls instead of pushing the filename
        # preview and the buttons out of the window.
        btn_row = ctk.CTkFrame(f, fg_color=_BG)
        btn_row.pack(side="bottom", fill="x", pady=(self._sp(2, 1), 0))

        self.save_btn = ctk.CTkButton(
            btn_row, text="Save & Organize", command=self._submit,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
            height=self._h(40, 34),
            font=ctk.CTkFont(size=self._h(14, 13), weight="bold"),
            text_color="#ffffff",
        )
        self.save_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.skip_btn = ctk.CTkButton(
            btn_row, text="Skip / Keep Original", command=self._skip,
            fg_color=_BG_FIELD, hover_color="#33334a", height=self._h(40, 34),
            font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED,
        )
        self.skip_btn.pack(side="left", expand=True, fill="x")

        # "Skip All & Delete" — skips every queued popup AND removes those
        # files from the watch folder. Only shown when the controller wires
        # on_skip_all (the popup releases itself before deleting so Windows
        # can remove the files — an open popup/preview keeps them locked).
        if self.on_skip_all is not None:
            self.skip_all_btn = ctk.CTkButton(
                btn_row, text="Skip All & Delete", command=self._skip_all,
                fg_color="#3a2b2b", hover_color="#4a3535", width=140,
                height=self._h(40, 34),
                font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
            )
            self.skip_all_btn.pack(side="left", fill="y", padx=(8, 0))

        # -- Live preview (PINNED, above the buttons) --------------------
        self.preview_label = ctk.CTkLabel(
            f, text="", font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
            wraplength=500, justify="left", height=self._h(28, 18),
        )
        self.preview_label.pack(side="bottom", fill="x", pady=(self._sp(4, 1), 0))

        # -- Serial number + Received copy (PINNED on short screens) -----
        # These two are created here — before the rest of the form — so that on
        # a short screen they can be pinned with side="bottom" ABOVE the
        # filename preview. Tk's packer squeezes whatever is packed LAST, and
        # the Received Copy checkbox used to be the casualty (a 2px sliver at
        # 1366x768, gone entirely below ~700). Pinned, the chip panel gives up
        # rows instead. On tall screens they stay in the normal top-down flow.
        serial_label = ctk.CTkLabel(f, text="Serial Number",
                                    font=ctk.CTkFont(size=13, weight="bold"),
                                    text_color=_TEXT_MUTED,
                                    height=self._h(28, 20))
        self.serial_entry = ctk.CTkEntry(
            f, textvariable=self._serial_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT, height=self._h(28, 24),
        )
        self.received_check = ctk.CTkCheckBox(
            f, text="Received Copy (unchecked = Submitted)",
            variable=self._received_var, fg_color=_ACCENT,
            hover_color=_ACCENT, text_color=_TEXT,
            checkbox_height=self._h(22, 20), checkbox_width=self._h(22, 20),
            font=ctk.CTkFont(size=self._h(13, 12)),
        )
        if self._compact:
            # side="bottom" stacks bottom-up: pack the checkbox first so it
            # ends up ABOVE the preview, then the entry, then its label.
            self.received_check.pack(side="bottom", anchor="w",
                                     pady=(0, self._sp(6)))
            self.serial_entry.pack(side="bottom", fill="x",
                                   pady=(0, self._sp(6)))
            serial_label.pack(side="bottom", anchor="w",
                              pady=(0, self._sp(1, 1)))

        # -- Target file banner -----------------------------------------
        self._banner = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=10)
        self._banner.pack(fill="x", pady=(0, self._sp(6)))

        banner_header = ctk.CTkFrame(self._banner, fg_color="transparent")
        banner_header.pack(fill="x", padx=12, pady=(self._sp(6), 0))
        self._banner_name = ctk.CTkLabel(
            banner_header, text="", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT, wraplength=380, justify="left",
            height=self._h(28, 22),
        )
        self._banner_name.pack(side="left", anchor="w")
        # Minimize: the popup (and its modal grab) must not block the user
        # from going elsewhere — the window minimizes to the taskbar and is
        # restored from there. Title-bar minimize works too.
        self.minimize_btn = ctk.CTkButton(
            banner_header, text="—", width=40, height=self._h(28, 24),
            fg_color=_BG_FIELD, hover_color="#33334a",
            text_color=_TEXT_MUTED, command=self._minimize_popup,
        )
        self.minimize_btn.pack(side="right", anchor="e", padx=(0, 6))
        self.preview_btn = ctk.CTkButton(
            banner_header, text="👁 Preview", width=96, height=self._h(28, 24),
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._toggle_preview,
        )
        self.preview_btn.pack(side="right", anchor="e")

        self._banner_size = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=12),
            text_color=_TEXT_MUTED, height=self._h(28, 18),
        )
        self._banner_size.pack(anchor="w", padx=12, pady=(0, self._sp(6)))

        # OCR status line — shows OCR progress, or the exact reason OCR is off
        # (disabled in config / no API key), never silently nothing. The
        # "↻ Retry OCR" button on the right appears once OCR finished (filled
        # or failed) and re-runs the vision call for this file.
        self._ocr_row = ctk.CTkFrame(f, fg_color="transparent")
        self._ocr_row.pack(fill="x", pady=(0, self._sp(2, 1)))
        self._ocr_label = ctk.CTkLabel(
            self._ocr_row, text="", font=ctk.CTkFont(size=11),
            text_color=_TEXT_MUTED, anchor="w", height=self._h(28, 18),
        )
        self._ocr_label.pack(side="left", fill="x", expand=True)
        self.retry_ocr_btn = ctk.CTkButton(
            self._ocr_row, text="↻ Retry OCR", width=92, height=self._h(22, 20),
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT,
            font=ctk.CTkFont(size=11), command=self._retry_ocr,
        )
        self.retry_ocr_btn.pack(side="right")
        self.retry_ocr_btn.pack_forget()  # shown only after OCR finished/failed

        # -- Company ----------------------------------------------------
        ctk.CTkLabel(f, text="Company", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED,
                     height=self._h(28, 20)).pack(anchor="w",
                                                  pady=(0, self._sp(1, 1)))
        self.company_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._company_var,
            command=self._on_company_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
            height=self._h(28, 24),
        )
        self.company_combo.pack(fill="x", pady=(0, self._sp(6)))

        # -- Client -----------------------------------------------------
        client_header = ctk.CTkFrame(f, fg_color="transparent")
        client_header.pack(fill="x", pady=(0, self._sp(1, 1)))
        ctk.CTkLabel(client_header, text="Client",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED,
                     height=self._h(28, 20)).pack(side="left")
        # 🗺 Map: alias a client name the document shows/OCR reads to another
        # client — future popups auto-switch the mapped name to its target.
        self.client_map_btn = ctk.CTkButton(
            client_header, text="🗺 Map", width=72, height=self._h(22, 20),
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: self._open_mapping_dialog("client"),
        )
        self.client_map_btn.pack(side="right")
        self.client_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_client_change,
        )
        self.client_dropdown.entry.configure(
            placeholder_text="Search client…", height=self._h(28, 24),
        )
        self.client_dropdown.pack_configure(pady=(0, self._sp(6)))

        # -- Site -------------------------------------------------------
        site_header = ctk.CTkFrame(f, fg_color="transparent")
        site_header.pack(fill="x", pady=(0, self._sp(1, 1)))
        ctk.CTkLabel(site_header, text="Site",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED,
                     height=self._h(28, 20)).pack(side="left")
        self.site_map_btn = ctk.CTkButton(
            site_header, text="🗺 Map", width=72, height=self._h(22, 20),
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: self._open_mapping_dialog("site"),
        )
        self.site_map_btn.pack(side="right")
        self.site_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_site_change,
        )
        self.site_dropdown.entry.configure(
            placeholder_text="Search site…", height=self._h(28, 24),
        )
        self.site_dropdown.pack_configure(pady=(0, self._sp(6)))

        # -- Document type ----------------------------------------------
        ctk.CTkLabel(f, text="Document Type", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED,
                     height=self._h(28, 20)).pack(anchor="w",
                                                  pady=(0, self._sp(1, 1)))
        self.doc_type_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._doc_type_var,
            command=lambda _d: self._refresh_preview(),
            fg_color=_BG_FIELD, button_color=_ACCENT, button_hover_color=_ACCENT,
            height=self._h(28, 24),
        )
        self.doc_type_combo.pack(fill="x", pady=(0, self._sp(6)))

        # -- Materials (multi-select) -----------------------------------
        material_header = ctk.CTkFrame(f, fg_color="transparent")
        material_header.pack(fill="x", pady=(0, self._sp(1, 1)))
        ctk.CTkLabel(material_header, text="Material (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED,
                     height=self._h(28, 20)).pack(side="left")
        # 🗺 Map: alias a word shown in the "Description of Goods" column to a
        # material ("nuts"/"bolts" -> Screw) — future OCR reads pre-select the
        # mapped material even when the document never writes its name.
        self.material_map_btn = ctk.CTkButton(
            material_header, text="🗺 Map", width=72, height=self._h(22, 20),
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: self._open_mapping_dialog("material"),
        )
        self.material_map_btn.pack(side="right")
        self.material_frame = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=8)
        if self._compact:
            # The panel is the only part of the form that can flex: with the
            # Serial/Received row pinned to the bottom, it absorbs whatever
            # space is left over instead of leaving a hole in the middle.
            self.material_frame.pack(fill="both", expand=True,
                                     pady=(0, self._sp(6)))
        else:
            self.material_frame.pack(fill="x", pady=(0, self._sp(6)))
        # Scrollable chip area (plain Canvas + scrollbar — the same pattern as
        # viewer.py): the chip rows pack into _material_inner and scroll when
        # they exceed the visible height (capped at _MATERIAL_ROWS_VISIBLE).
        self._material_canvas = tk.Canvas(
            self.material_frame, bg=_BG_SECONDARY, highlightthickness=0, bd=0,
            height=self._row_pitch * _MATERIAL_ROWS_VISIBLE,
        )
        self._material_vsb = ttk.Scrollbar(
            self.material_frame, orient="vertical", command=self._material_canvas.yview,
        )
        self._material_canvas.configure(yscrollcommand=self._material_vsb.set)
        self._material_canvas.pack(side="left", fill="both", expand=True,
                                   padx=(8, 0), pady=self._sp(6))
        self._material_vsb.pack(side="right", fill="y", padx=(0, 6),
                                pady=self._sp(6))
        self._material_inner = tk.Frame(self._material_canvas, bg=_BG_SECONDARY)
        self._mat_win = self._material_canvas.create_window(
            (0, 0), window=self._material_inner, anchor="nw",
        )
        self._material_inner.bind(
            "<Configure>",
            lambda _e: self._material_canvas.configure(
                scrollregion=self._material_canvas.bbox("all")),
        )
        self._material_canvas.bind(
            "<Configure>",
            lambda e: self._material_canvas.itemconfigure(self._mat_win, width=e.width),
        )
        # Mouse wheel over the panel scrolls it (chips are child widgets, so
        # a plain widget binding would never see the wheel event).
        def _material_wheel(event) -> None:
            try:
                if not self._material_canvas.winfo_exists():
                    return
                x = self._material_canvas.winfo_pointerx()
                y = self._material_canvas.winfo_pointery()
                wx = self._material_canvas.winfo_rootx()
                wy = self._material_canvas.winfo_rooty()
                if (wx <= x < wx + self._material_canvas.winfo_width()
                        and wy <= y < wy + self._material_canvas.winfo_height()):
                    self._material_canvas.yview_scroll(int(-event.delta / 120), "units")
            except tk.TclError:
                pass

        self._material_canvas.bind_all("<MouseWheel>", _material_wheel, add=True)
        self._material_chips: Dict[str, ctk.CTkButton] = {}
        # How many chip rows currently fit in the form: normally
        # _MATERIAL_ROWS_VISIBLE, fewer on short screens (recomputed on every
        # form resize — see _update_material_rows).
        self._material_rows_visible = _MATERIAL_ROWS_VISIBLE
        self._render_material_chips()
        self.form_frame.bind("<Configure>", self._update_material_rows)

        # -- Serial number + Received copy -------------------------------
        # (The widgets themselves are created with the pinned bottom bar at the
        # top of this method; on short screens they are already packed there
        # with side="bottom". On tall screens they flow normally, here, below
        # the material panel.)
        if not self._compact:
            serial_label.pack(anchor="w", pady=(0, 1))
            self.serial_entry.pack(fill="x", pady=(0, 6))
            self.received_check.pack(anchor="w", pady=(0, 6))

        # (The Save/Skip buttons and the filename preview are packed at the
        # TOP of this method with side="bottom" so they are pinned to the
        # bottom edge and always visible — see the "Bottom bar (PINNED)"
        # block. Only the live preview text needs refreshing here.)
        self._refresh_preview()
        # The <Configure> events that fired while the form was still being
        # built saw an incomplete widget list; recompute the chip rows once
        # the whole form exists and the geometry manager has settled.
        self.window.after_idle(self._update_material_rows)

    # ------------------------------------------------------------------
    # Config-driven state
    # ------------------------------------------------------------------
    def _reload_config_state(self) -> None:
        data = self.config.load()
        companies = data.get("companies", [])
        clients = data.get("clients", {})
        # Every material code is exactly two letters (see filename.material_code);
        # single-letter leftovers in the config ("A") are shown/used as "AL".
        self._materials_map = {
            name: fn.material_code(name, code)
            for name, code in dict(data.get("materials", {})).items()
        }
        self._rebuild_material_index()

        # Company dropdown (first entry is the default).
        company_names = list(companies)
        if company_names:
            self.company_combo.configure(values=company_names + [ADD_NEW_COMPANY_OPTION])
            self._company_var.set(company_names[0])
        else:
            self.company_combo.configure(values=[ADD_NEW_COMPANY_OPTION])
            self._company_var.set(ADD_NEW_COMPANY_OPTION)

        # Client dropdown — no default (empty) so the user must pick.
        # The first client is NOT auto-selected; the field stays empty until
        # the user searches/selects or creates a new client.
        client_names = list(clients.keys())
        if client_names:
            self.client_dropdown.configure(values=client_names + [ADD_NEW_CLIENT_OPTION])
            self.client_dropdown.set("")
            self._client_var.set("")
            self._populate_sites("")
        else:
            self.client_dropdown.configure(values=[ADD_NEW_CLIENT_OPTION])
            self.client_dropdown.set("")
            self._client_var.set("")

        doc_types = data.get("doc_types", ["DC"])
        self.doc_type_combo.configure(values=doc_types)
        self._doc_type_var.set(doc_types[0] if doc_types else "DC")

        self._render_material_chips()
        self._refresh_preview()

    def _populate_sites(self, client: str) -> None:
        sites = self.config.sites_for(client)
        values = sites + [ADD_NEW_SITE_OPTION]
        self.site_dropdown.configure(values=values)
        # Do NOT auto-select the first site — leave the box empty and let the
        # user pick. The add-new sentinel stays as a valid choice.
        self.site_dropdown.set("")

    # ------------------------------------------------------------------
    # Live config refresh (preserves what the user is typing)
    # ------------------------------------------------------------------
    def refresh_from_config(self) -> bool:
        """Reload dropdowns from (possibly fresh) config without losing typed text.

        Called after the popup's single live-config pull when something
        changed. Returns True if the UI changed.
        """
        try:
            data = self.config.load()
        except Exception:
            return False

        changed = False

        # -- Companies --------------------------------------------------
        companies = list(data.get("companies", []))
        try:
            cur_vals = list(self.company_combo.cget("values"))
            cur_no_sentinel = [c for c in cur_vals if c != ADD_NEW_COMPANY_OPTION]
        except Exception:
            cur_no_sentinel = []
        if companies != cur_no_sentinel:
            cur_company = self._company_var.get()
            new_vals = companies + [ADD_NEW_COMPANY_OPTION]
            self.company_combo.configure(values=new_vals)
            if cur_company not in companies and cur_company != ADD_NEW_COMPANY_OPTION \
                    and cur_company != getattr(self, "_ocr_company_override", None):
                if companies:
                    self._company_var.set(companies[0])
                else:
                    self._company_var.set(ADD_NEW_COMPANY_OPTION)
            # if cur_company still valid, keep it as-is (no set needed)
            changed = True

        # -- Doc types --------------------------------------------------
        doc_types = list(data.get("doc_types", ["DC"]))
        try:
            cur_doc_vals = list(self.doc_type_combo.cget("values"))
        except Exception:
            cur_doc_vals = []
        if doc_types != cur_doc_vals:
            cur_doc = self._doc_type_var.get()
            self.doc_type_combo.configure(values=doc_types)
            if cur_doc in doc_types:
                self._doc_type_var.set(cur_doc)
            elif doc_types:
                self._doc_type_var.set(doc_types[0])
            changed = True

        # -- Materials --------------------------------------------------
        new_materials = {
            name: fn.material_code(name, code)
            for name, code in dict(data.get("materials", {})).items()
        }
        if new_materials != self._materials_map:
            self._materials_map = new_materials
            # Keep only selected materials that still exist
            self._selected_materials = [m for m in self._selected_materials if m in new_materials]
            self._ocr_materials = {m for m in self._ocr_materials if m in new_materials}
            self._user_off_materials = {
                m for m in self._user_off_materials if m in new_materials}
            self._rebuild_material_index()
            self._render_material_chips()
            changed = True

        # -- Clients ----------------------------------------------------
        clients = data.get("clients", {})
        client_names = list(clients.keys())
        new_client_values = client_names + [ADD_NEW_CLIENT_OPTION]
        try:
            cur_client_vals = list(self.client_dropdown._values)
        except Exception:
            cur_client_vals = []
        if set(new_client_values) != set(cur_client_vals):
            self.client_dropdown.update_values_preserve_typed(new_client_values)
            resolved = self.client_dropdown.get()
            if resolved in client_names:
                self._client_var.set(resolved)
            # if resolved is a partial typed filter, leave _client_var as-is
            changed = True

        # -- Sites (always refresh — sites for the current client may have changed) --
        # Determine effective client for sites: prefer _client_var if it still exists
        effective_client = self._client_var.get()
        if effective_client not in clients:
            resolved_client = self.client_dropdown.get()
            if resolved_client in clients:
                effective_client = resolved_client
                # keep _client_var in sync when the dropdown resolved to a real client
                self._client_var.set(resolved_client)
            else:
                # No valid client — show only the sentinel; preserve whatever the user typed in site
                effective_client = ""

        sites = self.config.sites_for(effective_client) if effective_client else []
        new_site_values = sites + [ADD_NEW_SITE_OPTION]
        try:
            cur_site_vals = list(self.site_dropdown._values)
        except Exception:
            cur_site_vals = []
        if set(new_site_values) != set(cur_site_vals):
            self.site_dropdown.update_values_preserve_typed(new_site_values)
            changed = True
        elif effective_client and not self.site_dropdown.entry.get().strip():
            # Ensure empty site box stays empty (populate already did)
            pass

        if changed:
            self._refresh_preview()
            # Refresh open dropdown listboxes if filtered
            try:
                if self.client_dropdown._dropdown_open:
                    self.client_dropdown._update_listbox()
                    self.client_dropdown._reposition()
                if self.site_dropdown._dropdown_open:
                    self.site_dropdown._update_listbox()
                    self.site_dropdown._reposition()
            except Exception:
                pass

        return changed

    def _start_config_poll(self) -> None:
        """Schedule the ONE live-config pull for when this popup opens."""
        self._config_poll_after = None
        if not self.config.enable_live_config:
            return
        # A single fetch shortly after open so the popup never shows stale
        # data — deliberately NOT re-armed: the config must not be pulled
        # while the user is editing config.json or force-pushing from the
        # tray (a background pull kept reverting hand-made deletions).
        try:
            self._config_poll_after = self.window.after(500, self._poll_config)
        except Exception:
            pass

    def _stop_config_poll(self) -> None:
        after = getattr(self, "_config_poll_after", None)
        if after is not None:
            try:
                self.window.after_cancel(after)
            except Exception:
                pass
            self._config_poll_after = None

    def _poll_config(self) -> None:
        """One background fetch → apply → refresh (never blocks the UI)."""
        if not self.config.enable_live_config:
            return

        def work() -> None:
            try:
                changed = self.config.sync_from_github(timeout=5.0)
                if changed:
                    try:
                        self.window.after(0, self.refresh_from_config)
                    except tk.TclError:
                        pass
            except Exception as exc:
                print(f"[filepicker] popup config poll error: {exc}")

        threading.Thread(target=work, name="filepicker-popup-config", daemon=True).start()

    # ------------------------------------------------------------------
    # Material chip rendering
    # ------------------------------------------------------------------
    def _render_material_chips(self) -> None:
        for child in self._material_inner.winfo_children():
            child.destroy()
        self._material_chips.clear()

        # Measure text so chips pack tightly with no big gaps between them.
        measure_font = tkfont.Font(family="Segoe UI", size=13)
        wrap_width = 500
        row_frame = ctk.CTkFrame(self._material_inner, fg_color="transparent")
        row_frame.pack(fill="x", padx=8, pady=6)
        row_width = 0
        rows = 1

        def place_chip(text, fg, hover, txt, command):
            nonlocal row_frame, row_width, rows
            est = measure_font.measure(text) + 28  # text + padding
            # Wrap BEFORE constructing the chip: the chip must become a child
            # of the row it packs into. The old order (chip first, wrap after)
            # packed the chip into the previous row AND left an empty row
            # frame behind it — a childless CTkFrame requests 200px height, so
            # the materials section ballooned and pushed the serial number /
            # Save buttons out of the (fixed-size, non-scrollable) popup.
            if row_width + est > wrap_width:
                row_frame = ctk.CTkFrame(self._material_inner, fg_color="transparent")
                row_frame.pack(fill="x", padx=8, pady=(0, 6))
                row_width = 0
                rows += 1
            chip = ctk.CTkButton(
                row_frame, text=text, width=0, height=self._h(28, 24),
                fg_color=fg, hover_color=hover, text_color=txt,
                corner_radius=14, command=command,
            )
            chip.pack(side="left", padx=(0, 6))
            row_width += est + 6
            return chip

        # Display order: selected materials move to the top row (cosmetic
        # only — the config order is never changed); each group is sorted
        # alphabetically by shortcode, so deselecting returns the chip to
        # its sorted position.
        for name, code in material_display_order(self._materials_map, self._selected_materials):
            selected_now = name in self._selected_materials
            chip = place_chip(
                f"{name} ({code})",
                _ACCENT if selected_now else _BG_FIELD,
                _ACCENT_HOVER if selected_now else "#33334a",
                "#ffffff" if selected_now else _TEXT,
                lambda n=name: self._toggle_material(n),
            )
            self._material_chips[name] = chip

        place_chip(
            "+ Add Material", _BG_FIELD, "#33334a", _ACCENT,
            self._prompt_add_material,
        )

        # Cap the visible chip area at the rows that fit (normally
        # _MATERIAL_ROWS_VISIBLE, fewer on short screens); extra rows scroll
        # (mouse wheel over the panel). Shrinks to fit small catalogs.
        # Height = pitch * rows: the pitch (28px chip + 6px gap + slack, or 24
        # + 6 in the compact layout) means the full last row is visible
        # without extra top padding.
        self._material_canvas.configure(
            height=self._row_pitch
            * min(rows, getattr(self, "_material_rows_visible",
                                _MATERIAL_ROWS_VISIBLE))
        )

    def _update_material_rows(self, _event=None) -> None:
        """Show as many material chip rows as the form has room for.

        The bottom bar (filename preview + Save/Skip) is PINNED to the bottom
        of the popup, so on short screens the chip panel must give up rows:
        it shrinks and its scrollbar (or the mouse wheel) reaches the hidden
        chips, instead of the middle of the form pushing the filename preview
        and buttons off the bottom of the window. Recomputed on every form
        resize; capped at _MATERIAL_ROWS_VISIBLE so big screens are unchanged.
        """
        try:
            avail = self.form_frame.winfo_height()
        except tk.TclError:
            return
        if avail <= 1:
            return
        # Height used by everything except the chip panel: the widgets packed
        # top-down (banner, form fields, section labels) AND the ones pinned
        # with side="bottom" (chip panel aside: the filename preview, the
        # Serial field and the Received Copy checkbox, plus the buttons on a
        # short screen). Both are reserved by Tk out of the form's height, so
        # the chip panel only gets what is genuinely left over.
        bottom = 0
        others = 0
        for child in self.form_frame.pack_slaves():
            if child is self.material_frame:
                continue
            try:
                info = child.pack_info()
            except Exception:
                continue
            used = child.winfo_reqheight()
            pady = info.get("pady", 0)
            if isinstance(pady, (tuple, list)):
                used += sum(int(p) for p in pady)
            else:
                used += 2 * int(pady or 0)
            if info.get("side") == "bottom":
                bottom += used
            else:
                others += used
        # 12px slack so an estimate error never clips the last row.
        rows = int((avail - bottom - others - 12) // self._row_pitch)
        rows = max(1, min(_MATERIAL_ROWS_VISIBLE, rows))
        if rows != self._material_rows_visible:
            self._material_rows_visible = rows
            self._render_material_chips()

    def _toggle_material(self, name: str) -> None:
        if name in self._selected_materials:
            self._selected_materials.remove(name)
            # A hand toggle makes the material the USER's choice: OCR never
            # removes it again, and a deselected one is never re-added.
            self._ocr_materials.discard(name)
            self._user_off_materials.add(name)
        else:
            self._selected_materials.append(name)
            self._ocr_materials.discard(name)
            self._user_off_materials.discard(name)
        self._render_material_chips()
        self._refresh_preview()

    # ------------------------------------------------------------------
    # "Description of Goods" -> catalog materials
    # ------------------------------------------------------------------
    def _goods_material_matches(self, goods_text) -> List[str]:
        """The ONE catalog material each goods item refers to.

        OCR transcribes ONLY the bold heading words of the "Description of
        Goods" table (see the OCR prompt) — the item names, never the
        sub-description lines printed below them. Each item heading names ONE
        material, so every heading is matched on its own and only its best
        match is taken: "SS Spigot" is a spigot (the mapped word, six
        characters) made of SS (a two-letter code), so longest-match wins and
        only "fitting" is selected — reading the whole goods text as one blob
        used to add "Stainless Steel" as a second, wrong material for the same
        line. Headings are separated by commas (what the OCR prompt asks for),
        newlines, semicolons, pipes or bullets.

        Within one heading the matching is deliberately conservative:

        - whole words only (``\\b``), so "stal" never matches "Stainless" and
          "customer" never matches the "CO" code;
        - a trailing plural is allowed ("Screws" -> "Screw");
        - both the material NAME and its shortcode are searched ("Mild Steel"
          or "MS"), which is how delivery notes usually write items;
        - material MAPPINGS (the 🗺 Map editor) are applied: a mapped goods
          word ("nuts"/"bolts" -> Screw) selects its material even though the
          document never writes the material name;
        - the longest match wins (a NAME before a mapped word before a code on
          a tie), so "GI SHEET" selects only "GI SHEET" and not the "GI" code
          of "Galvanized Iron".

        Returns the matched material names in the order the items appear in
        the goods text (empty when nothing matches — the caller then leaves the
        material selection untouched).
        """
        raw = str(goods_text or "").strip()
        if not raw:
            return []
        chosen: List[str] = []
        for item in _GOODS_ITEM_SPLIT.split(raw):
            name = self._best_material_in_item(item)
            if name and name not in chosen:
                chosen.append(name)
        return chosen

    def _best_material_in_item(self, item: str) -> Optional[str]:
        """The single best catalog material named by ONE goods item heading.

        Candidates are every catalog material whose name, shortcode or mapped
        synonym appears in the heading; the most specific one (longest span,
        then NAME > mapped word > code) is the material the item is about.
        Returns None when the heading mentions no catalog material.
        """
        text = re.sub(r"[^a-z0-9]+", " ", str(item or "").lower())
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None

        # (start, end, rank, material_name); rank: 0 = material NAME,
        # 1 = mapped goods word, 2 = shortcode.
        candidates = []
        for name, code in self._materials_map.items():
            terms = [(str(name), 0)]
            if code and len(str(code)) >= 2:
                terms.append((str(code), 2))
            for term, rank in terms:
                words = [w for w in re.split(r"[^A-Za-z0-9]+", term.lower()) if w]
                if not words:
                    continue
                if len(words) == 1 and len(words[0]) < 2:
                    continue  # a single letter is never a material signal
                pieces = []
                for word in words:
                    alts = _MATERIAL_WORD_ALIASES.get(word) if rank == 0 else None
                    if alts:
                        pieces.append("(?:" + "|".join(
                            re.escape(w) for w in (word,) + tuple(alts)) + ")")
                    else:
                        pieces.append(re.escape(word))
                pattern = (r"\b" + r"\s+".join(pieces) + r"(?:s|es)?\b")
                try:
                    for m in re.finditer(pattern, text):
                        candidates.append((m.start(), m.end(), rank, name))
                except re.error:
                    continue

        # Material MAPPINGS (the 🗺 Map button): a goods word like "nuts" or
        # "bolts" maps to a catalog material ("Screw") even though the
        # document never writes its name. Aliases only ever point at existing
        # catalog materials; a mapped target that is not in the catalog is
        # ignored (nothing that cannot be selected is invented).
        try:
            config = getattr(self, "config", None)
            aliases = dict(config.material_aliases) if config is not None else {}
        except Exception:
            aliases = {}
        for src, tgt in aliases.items():
            tgt_name = str(tgt or "").strip()
            if tgt_name not in self._materials_map:
                continue
            words = [w for w in re.split(r"[^A-Za-z0-9]+", str(src).lower()) if w]
            if not words or (len(words) == 1 and len(words[0]) < 2):
                continue
            pieces = []
            for idx, word in enumerate(words):
                if (idx == len(words) - 1 and word.endswith("s")
                        and len(word) > 2):
                    # "nuts" also matches "nut" (singular strip on the last
                    # word), so a "nuts" -> Screw mapping catches both.
                    pieces.append("(?:" + re.escape(word) + "|"
                                  + re.escape(word[:-1]) + ")")
                else:
                    pieces.append(re.escape(word))
            pattern = r"\b" + r"\s+".join(pieces) + r"(?:s|es)?\b"
            try:
                for m in re.finditer(pattern, text):
                    candidates.append((m.start(), m.end(), 1, tgt_name))
            except re.error:
                continue

        if not candidates:
            return None
        # Longest span first, then the most specific kind of match, then the
        # earliest one — the winner is the material this item is about.
        candidates.sort(key=lambda c: (-(c[1] - c[0]), c[2], c[0]))
        return candidates[0][3]

    def _apply_goods_materials(self, goods_text) -> bool:
        """Pre-select the catalog materials the goods description mentions.

        Only OCR's OWN previous selection is replaced: the materials the last
        read added are dropped and the new matches take their place, while
        every material the user picked (or deselected) by hand survives
        untouched. That is what makes ↻ Retry able to fix a wrong material
        list — the old rule ("if anything is already selected, do nothing")
        made a retry a no-op for materials.

        A goods text that matches nothing at all only clears OCR's own picks
        (the new read is the truth); the user's selection is left exactly as
        it is — no material is ever invented or renamed.
        """
        matches = self._goods_material_matches(goods_text)
        # The user's materials: everything selected that OCR did not put there.
        keep = [m for m in self._selected_materials if m not in self._ocr_materials]
        add = [m for m in matches
               if m not in keep and m not in self._user_off_materials]
        new_selection = keep + add
        self._ocr_materials = set(add)
        if new_selection == self._selected_materials:
            return False
        self._selected_materials = new_selection
        try:
            self._render_material_chips()
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Hotkeys (Alt + 2-letter code) — work from any field
    # ------------------------------------------------------------------
    # Alt+RC toggles the Received-copy checkbox (the "RC" pseudo-code; no
    # material uses it).
    _ALT_ACTION_CODE = "RC"

    def _on_alt_key(self, event) -> None:
        """Alt + <2-letter code> toggles a material (Alt+AL = Aluminium).

        The user holds Alt and taps the two letters of the material's code;
        the first pair that forms a known code toggles it. Bound on the
        window, so it works while any field (client search, serial, ...)
        has focus — not just the materials area. Alt+RC toggles the
        Received-copy checkbox instead.
        """
        keysym = getattr(event, "keysym", "") or ""
        self._alt_seq, matched = alt_seq_step(self._alt_seq, keysym)
        if matched:
            name = self._material_by_code.get(matched)
            if name is not None:
                self._toggle_material(name)
            elif matched == self._ALT_ACTION_CODE:
                self._received_var.set(not self._received_var.get())
        self._arm_alt_timer()

    def _on_alt_release(self, _event=None) -> None:
        """Decide what survives an Alt release.

        A half-typed chord survives only when the pending letter could form
        a same-letter code (SS, TT, ...): those two keys physically cannot
        be pressed together, so Alt+S, release, Alt+S within a second must
        still toggle Stainless Steel. Everything else resets on release.
        """
        if self._alt_seq and (self._alt_seq.upper() * 2) in self._material_by_code:
            # Keep the pending letter alive for the second chord — the
            # timer runs from this release (1s window, as requested).
            self._arm_alt_timer()
        else:
            self._reset_alt_seq()

    def _on_any_key(self, event) -> None:
        """Plain KeyPress catcher — makes RIGHT Alt chords work.

        Tk on Windows does not reliably set its Alt modifier for the right
        Alt key, so letters pressed with right Alt held arrive here without
        ever firing the <Alt-KeyPress> binding. The physical Alt_R key is
        tracked instead; its letters are routed into the same chord logic.
        """
        keysym = getattr(event, "keysym", "") or ""
        if keysym == "Alt_R":
            self._right_alt_down = True
            return
        if not self._right_alt_down:
            return
        if event.state & 0x8:  # Mod1 set -> <Alt-KeyPress> already handled it
            return
        self._on_alt_key(event)

    def _on_any_key_release(self, event) -> None:
        if getattr(event, "keysym", "") != "Alt_R":
            return
        if not self._right_alt_down:
            return
        self._right_alt_down = False
        # Same pending-chord semantics as a left-Alt release.
        self._on_alt_release()

    def _arm_alt_timer(self, ms: int = 1000) -> None:
        """(Re)arm the pending-chord timeout; expiry discards it."""
        if self._alt_seq_after is not None:
            try:
                self.window.after_cancel(self._alt_seq_after)
            except Exception:
                pass
        self._alt_seq_after = self.window.after(ms, self._reset_alt_seq)

    def _reset_alt_seq(self) -> None:
        self._alt_seq = ""
        if self._alt_seq_after is not None:
            try:
                self.window.after_cancel(self._alt_seq_after)
            except Exception:
                pass
            self._alt_seq_after = None

    def _rebuild_material_index(self) -> None:
        """Map normalized 2-letter code -> material name (for Alt hotkeys)."""
        self._material_by_code = {}
        for name, code in self._materials_map.items():
            key = str(code).strip().upper()
            if key and key not in self._material_by_code:
                self._material_by_code[key] = name

    # ------------------------------------------------------------------
    # Minimize support
    # ------------------------------------------------------------------
    def _minimize_popup(self) -> None:
        """Minimize the popup to the taskbar (restore from the taskbar entry)."""
        try:
            self.window.iconify()
        except tk.TclError:
            pass

    def _ensure_popup_visible(self) -> None:
        """Watchdog: the popup must really be on screen.

        CustomTkinter withdraws every new window for a few milliseconds to
        colour its title bar and re-shows it from an ``after(5)`` callback. If
        anything disturbs that dance the window can stay hidden while Tk still
        believes it is mapped — the popup is then invisible but alive, and
        because the controller waits for it before showing the next one, NO
        popup would ever appear again while OCR kept running in the
        background. This check (a few attempts, a moment apart) re-shows such
        a window and says so in the log, so the worst case is a popup that
        appears a second late instead of a queue that silently stops.

        A popup the user minimised on purpose (``iconic``) is left alone.
        """
        try:
            if not self.window.winfo_exists():
                return
            state = str(self.window.state())
        except Exception:
            return
        if state == "iconic":
            return
        if winfocus.visible(self.window):
            return
        print(f"[filepicker] popup window was not visible (state={state!r}) — "
              f"re-showing it")
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.attributes("-topmost", True)
        except Exception as exc:
            print(f"[filepicker] could not re-show the popup: {exc}")
        if not winfocus.visible(self.window):
            # Tk thinks it is mapped while Win32 keeps it hidden: force a real
            # re-map (safe here — the titlebar dance is long over).
            try:
                self.window.withdraw()
                self.window.update_idletasks()
                self.window.deiconify()
                self.window.lift()
            except Exception as exc:
                print(f"[filepicker] could not re-map the popup: {exc}")
        try:
            winfocus.claim(self.window)
        except Exception:
            pass
        self._visible_checks_left = getattr(self, "_visible_checks_left", 1) - 1
        if self._visible_checks_left > 0:
            try:
                self.window.after(
                    _POPUP_VISIBLE_CHECK_MS, self._ensure_popup_visible)
            except Exception:
                pass

    def _prompt_add_material(self) -> None:
        self._ask_text(
            "Add Material",
            "Material name:",
            default="",
            placeholder="e.g. Copper",
            on_ok=self._add_material,
        )

    def _add_material(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        # Auto-derive a 2-letter shortcode from the name if not provided.
        shortcode = self._derive_shortcode(name)
        self.config.add_material(name, shortcode)
        self._materials_map = {
            n: fn.material_code(n, c)
            for n, c in self.config.materials.items()
        }
        self._rebuild_material_index()
        if name not in self._selected_materials:
            self._selected_materials.append(name)
        self._render_material_chips()
        self._refresh_preview()

    @staticmethod
    def _derive_shortcode(name: str) -> str:
        # Every material code is exactly two letters (e.g. "Aluminium" -> "AL",
        # "Galvanized Iron" -> "GI"), derived from the material name.
        return fn.material_code(name)

    # ------------------------------------------------------------------
    # Dropdown handlers
    # ------------------------------------------------------------------
    def _on_company_change(self, company: str) -> None:
        if not company or company == "(no companies)":
            return
        # A menu pick overrides any OCR-placed (non-catalog) company.
        self._ocr_company_override = None
        if company == ADD_NEW_COMPANY_OPTION:
            self._ask_text(
                "Add New Company",
                "New company name:",
                default="",
                placeholder="e.g. Acme Corp",
                on_ok=self._add_new_company,
            )
            return
        self._refresh_preview()

    def _add_new_company(self, company: str) -> None:
        company = company.strip()
        if not company:
            # Nothing entered; revert to the previously selected company.
            self._reload_company_options()
            return
        self.config.add_company(company)
        self._reload_company_options()
        self._company_var.set(company)
        self._refresh_preview()

    def _reload_company_options(self) -> None:
        companies = self.config.companies
        self.company_combo.configure(values=companies + [ADD_NEW_COMPANY_OPTION])
        if companies:
            self._company_var.set(companies[0])

    def _on_client_change(self, client: str) -> None:
        if not client or client == "(no clients)":
            return
        if client == ADD_NEW_CLIENT_OPTION:
            self._ask_text(
                "Add New Client",
                "New client name:",
                default="",
                placeholder="e.g. Gamma Projects",
                on_ok=self._add_new_client,
            )
            return
        # Keep the internal var in sync so the add-new-site flow uses the
        # currently selected client.
        self._client_var.set(client)
        self._populate_sites(client)
        self._refresh_preview()

    def _add_new_client(self, client: str) -> None:
        client = client.strip()
        if not client:
            # Nothing entered; revert to the previously selected client.
            self._reload_client_options()
            return
        # add_client dedupes near-same clients and returns the name to use
        # (existing canonical spelling, or the newly added one).
        effective = self.config.add_client(client)
        self._reload_client_options()
        self.client_dropdown.set(effective)
        self._populate_sites(effective)
        self._refresh_preview()

    def _reload_client_options(self) -> None:
        clients = self.config.clients
        self.client_dropdown.configure(values=list(clients.keys()) + [ADD_NEW_CLIENT_OPTION])
        if clients:
            self.client_dropdown.set(next(iter(clients)))

    def _on_site_change(self, site: str) -> None:
        if site == ADD_NEW_SITE_OPTION:
            client = self._client_var.get()
            self._ask_text(
                "Add New Site",
                f"New site name for {client}:",
                default="",
                placeholder="e.g. Site 3 - Delhi",
                on_ok=self._add_new_site,
            )
            return
        self._refresh_preview()

    def _add_new_site(self, site: str) -> None:
        site = site.strip()
        if not site:
            # Nothing entered; revert to previous selection.
            self._populate_sites(self._client_var.get())
            return
        client = self._client_var.get()
        # add_site dedupes near-same sites and returns the name to use
        # (existing canonical spelling, or the newly added one).
        effective = self.config.add_site(client, site)
        self._populate_sites(client)
        self.site_dropdown.set(effective)
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Name MAPPING (the 🗺 Map buttons next to Client/Site)
    # ------------------------------------------------------------------
    def _open_mapping_dialog(self, kind: str) -> None:
        """Open the Map <Client|Site|Material> editor for *kind*.

        The dialog lists the existing mappings (searchable) and lets the
        user map the name the current document shows (pre-filled from the
        field) to the name to use instead. Future OCR reads of the mapped
        source switch to the target automatically. For materials, the
        SOURCE is a word printed in the "Description of Goods" column and
        the TARGET is the catalog material to pre-select for it.
        """
        if kind == "client":
            names = list(self.config.clients.keys())
            current = self.client_dropdown.get().strip()
        elif kind == "material":
            names = list(self.config.materials.keys())
            current = self._selected_materials[0] \
                if self._selected_materials else ""
        else:
            names = self.config.all_sites()
            current = self._current_site().strip()
        MappingDialog(
            self.window, kind=kind, config=self.config,
            names=names, current=current,
            on_changed=lambda k=kind: self._apply_mapping_to_current(k),
        )

    def _apply_mapping_to_current(self, kind: str) -> None:
        """After a mapping is added/removed, switch the popup's field if it
        shows the (now mapped) source name.

        The mapping is "the name this document has → the name to use", so a
        just-mapped source visible in the current popup switches to the
        target right away — the same rule OCR will follow for future popups.
        """
        if kind == "material":
            # Material mappings affect future OCR reads of the goods
            # description; there is no popup field to switch right now.
            return
        try:
            if kind == "client":
                cur = self.client_dropdown.get().strip()
                mapped = self.config.resolve_client(cur)
                if mapped and mapped != cur:
                    self.client_dropdown.set(mapped)
                    self._client_var.set(mapped)
                    self._populate_sites(mapped)
                    self._refresh_preview()
            else:
                cur = self._current_site().strip()
                mapped = self.config.resolve_site(cur)
                if mapped and mapped != cur:
                    client = self._client_var.get().strip()
                    # Read-only resolution: a mapped target that isn't in
                    # this client's sites yet is only ADDED on Save, never
                    # when the mapping dialog is used.
                    site = self._resolve_site_readonly(client, mapped) \
                        if client else mapped
                    self.site_dropdown.set(site)
                    self._refresh_preview()
        except Exception as exc:
            print(f"[filepicker] mapping apply error: {exc}")

    # ------------------------------------------------------------------
    # Generic small inline prompt
    # ------------------------------------------------------------------
    def _ask_text(self, title: str, label: str, default: str, placeholder: str,
                  on_ok: Callable[[str], None]) -> None:
        prompt = ctk.CTkToplevel(self.window)
        prompt.title(title)
        prompt.configure(fg_color=_BG)
        prompt.transient(self.window)
        prompt.grab_set()
        prompt.attributes("-topmost", True)

        # Center the prompt over the parent popup instead of the screen corner.
        self.window.update_idletasks()
        pw, ph = 380, 150
        x = self.window.winfo_rootx() + max((self.window.winfo_width() - pw) // 2, 0)
        y = self.window.winfo_rooty() + max((self.window.winfo_height() - ph) // 2, 0)
        prompt.geometry(f"{pw}x{ph}+{x}+{y}")

        ctk.CTkLabel(prompt, text=label, font=ctk.CTkFont(size=13),
                     text_color=_TEXT).pack(anchor="w", padx=16, pady=(16, 8))
        entry = ctk.CTkEntry(prompt, fg_color=_BG_FIELD, border_color=_BG_FIELD,
                             text_color=_TEXT, placeholder_text=placeholder)
        entry.insert(0, default)
        entry.pack(fill="x", padx=16, pady=(0, 12))
        entry.focus_set()

        def confirm() -> None:
            value = entry.get()
            prompt.destroy()
            on_ok(value)

        def cancel() -> None:
            prompt.destroy()

        entry.bind("<Return>", lambda _e: confirm())
        ctk.CTkButton(prompt, text="OK", command=confirm, fg_color=_ACCENT,
                      width=100).pack(side="left", padx=(16, 8), pady=(0, 16))
        ctk.CTkButton(prompt, text="Cancel", command=cancel,
                      fg_color=_BG_FIELD, text_color=_TEXT_MUTED,
                      width=100).pack(side="left", pady=(0, 16))

    # ------------------------------------------------------------------
    # OCR auto-fill (OpenCode Go "DeepSeek V4.1 Flash")
    # ------------------------------------------------------------------
    def _set_ocr_status(self, text: str, color: str = _TEXT_MUTED) -> None:
        label = getattr(self, "_ocr_label", None)
        if label is None:
            return
        try:
            label.configure(text=text, text_color=color)
        except Exception:
            pass

    def _ocr_pool_finished(self, pool) -> bool:
        """True when the pool already has an OUTCOME for this file.

        ``pool.get()`` returns None both for "not read yet" and for "read but
        nothing could be extracted"; without this distinction a file that was
        already read (and failed) opened with a "reading document…" line that
        looked like OCR had restarted. Pools without the accessor (older/fake
        ones) simply report False.
        """
        finished = getattr(pool, "finished", None)
        if not callable(finished):
            return False
        try:
            return bool(finished(self.file_path))
        except Exception:
            return False

    def _ocr_reading_text(self) -> str:
        """The "reading document…" line, with live counters and elapsed time.

        Every download is read simultaneously (the pool is submitted for the
        whole batch the moment the files land), so the counters tell the user
        that the OTHER files are being read too — this line is not a stuck
        "processing" for this one file. The seconds tick up as well, so a slow
        read is visibly progressing instead of looking frozen.
        """
        pool = getattr(self, "ocr_pool", None)
        running = queued = 0
        progress = getattr(pool, "progress", None)
        if callable(progress):
            try:
                running, queued = progress()
            except Exception:
                running = queued = 0
        waited = ""
        started = getattr(self, "_ocr_wait_started", None)
        if started is not None:
            try:
                secs = time.monotonic() - started
            except Exception:
                secs = 0.0
            if secs >= 2.0:
                waited = f" {secs:.0f}s"
        if queued:
            return (f"OCR: reading document…{waited} "
                    f"({running} reading, {queued} queued)")
        if running > 1:
            return f"OCR: reading document…{waited} ({running} files read together)"
        return f"OCR: reading document…{waited}"

    def _start_ocr_progress(self) -> None:
        """Refresh the status line every :data:`_OCR_PROGRESS_MS` while reading."""
        self._stop_ocr_progress()
        self._ocr_poll_done = False
        self._ocr_wait_started = time.monotonic()
        try:
            self._ocr_progress_after = self.window.after(
                _OCR_PROGRESS_MS, self._tick_ocr_progress)
        except tk.TclError:
            self._ocr_progress_after = None

    def _tick_ocr_progress(self) -> None:
        self._ocr_progress_after = None
        if getattr(self, "_ocr_poll_done", True):
            return
        # Safety net: the pool's completion callback is delivered from a
        # worker thread through window.after(); if that ever fails to land (a
        # Tk cross-thread hiccup) the popup would sit on "reading document…"
        # for a file that is already read. This tick runs on the UI thread, so
        # it picks the outcome up itself the moment the pool has it — a file
        # can never stay stuck on "processing OCR" once its read is done.
        pool = getattr(self, "ocr_pool", None)
        if pool is not None and self._ocr_pool_finished(pool):
            self._apply_ocr_outcome(pool.get(self.file_path))
            return
        self._set_ocr_status(self._ocr_reading_text(), _ACCENT)
        try:
            self._ocr_progress_after = self.window.after(
                _OCR_PROGRESS_MS, self._tick_ocr_progress)
        except tk.TclError:
            self._ocr_progress_after = None

    def _stop_ocr_progress(self) -> None:
        """Cancel the pending status tick (idempotent, never raises)."""
        after = getattr(self, "_ocr_progress_after", None)
        self._ocr_progress_after = None
        if after is None:
            return
        try:
            self.window.after_cancel(after)
        except Exception:
            pass

    def _set_ocr_retry_visible(self, visible: bool) -> None:
        """Show/hide the "↻ Retry OCR" button (visible once OCR finished)."""
        btn = getattr(self, "retry_ocr_btn", None)
        if btn is None:
            return
        try:
            if visible:
                btn.pack(side="right")
            else:
                btn.pack_forget()
        except tk.TclError:
            pass

    @staticmethod
    def _short_ocr_error(err: str) -> str:
        """A one-line popup status for an OCR failure message.

        "OpenCode Go API error (500): {...}" becomes
        "OCR failed (API error 500) — click ↻ Retry OCR". Anything else is
        generic. The full message stays in the log.
        """
        if err.startswith("OpenCode Go API error ("):
            rest = err[len("OpenCode Go API error ("):]
            code = rest.split(")", 1)[0] if ")" in rest else ""
            if code.isdigit():
                return f"OCR failed (API error {code}) — click ↻ Retry OCR"
        return "OCR failed — click ↻ Retry OCR"

    def _retry_ocr(self) -> None:
        """Re-run OCR for this file (the "↻ Retry OCR" button).

        The pool forgets the cached result and makes a fresh vision call that
        THINKS (see ocr.retry_thinking_level): the user pressed this because
        the first answer was wrong, so repeating the identical fast call would
        just repeat the same wrong fields. The new outcome REPLACES the values
        the previous read had filled — fields the user typed or toggled are
        still never clobbered.
        """
        pool = getattr(self, "ocr_pool", None)
        if pool is None or not pool.available:
            return
        try:
            self.retry_ocr_btn.configure(state="disabled")
        except Exception:
            pass
        self._ocr_retrying = True
        self._set_ocr_status(
            "OCR: retrying — reading it again, more carefully…", _ACCENT)
        self._start_ocr_progress()

        def on_done(result) -> None:
            def apply() -> None:
                try:
                    if not self.window.winfo_exists():
                        return
                except tk.TclError:
                    return
                try:
                    self.retry_ocr_btn.configure(state="normal")
                except Exception:
                    pass
                self._apply_ocr_outcome(result)

            try:
                self.window.after(0, apply)
            except tk.TclError:
                pass

        level = None
        try:
            from ocr import retry_thinking_level
            level = retry_thinking_level(
                getattr(self.config, "ocr_thinking", None))
        except Exception:
            level = None
        try:
            pool.retry(self.file_path, on_done, thinking=level)
        except TypeError:
            # A pool that predates the per-read thinking override.
            pool.retry(self.file_path, on_done)

    def _start_ocr(self) -> None:
        """Consume the background OCR result for this file (if any).

        EVERY completed download is submitted to the controller's OcrPool the
        moment it lands, all together, so by the time a popup opens the result
        is normally already cached — including for the files further down the
        queue. If this one is still in flight we subscribe to its completion
        and keep a live "N files read together" counter on the status line;
        results are applied on the UI thread and never clobber anything the
        user already typed (values a PREVIOUS read filled are replaced — see
        _ocr_may_replace).
        """
        pool = getattr(self, "ocr_pool", None)
        if pool is None:
            # The controller only wires the pool when enable_ocr is true in
            # config.json — say so instead of failing silently.
            self._set_ocr_status(
                "OCR: disabled — set \"enable_ocr\": true in config.json"
            )
            return
        if not pool.available:
            self._set_ocr_status(
                "OCR: no API key — put opencode_token.txt next to the exe "
                "(or set OPENCODE_API_KEY)"
            )
            return

        cached = pool.get(self.file_path)
        if cached is not None or self._ocr_pool_finished(pool):
            self._apply_ocr_outcome(cached)
            return

        self._set_ocr_status(self._ocr_reading_text(), _ACCENT)
        self._start_ocr_progress()

        def on_done(result) -> None:
            def apply() -> None:
                try:
                    if not self.window.winfo_exists():
                        return
                except tk.TclError:
                    return
                self._apply_ocr_outcome(result)

            try:
                self.window.after(0, apply)
            except tk.TclError:
                pass

        pool.submit(self.file_path, on_done)

    def _ocr_seconds(self) -> str:
        """``" in 3.2s"`` for the last read of this file ("" when unknown).

        The pool records how long each read took; showing it next to the
        filled fields makes the effect of the speed settings visible at a
        glance (pools without the accessor — older/fake ones — add nothing).
        """
        pool = getattr(self, "ocr_pool", None)
        duration = getattr(pool, "duration", None)
        if not callable(duration):
            return ""
        try:
            seconds = duration(self.file_path)
        except Exception:
            return ""
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return ""
        return f" in {seconds:.1f}s"

    def _ocr_may_replace(self, field: str, current: str) -> bool:
        """True when OCR may (over)write *field*, which currently shows *current*.

        Empty is ours to fill, and a value an EARLIER OCR read put there is
        ours to correct — that is what lets the ↻ Retry button fix a wrong
        read. Anything else is the user's own text and is never touched.
        """
        current = (current or "").strip()
        if not current:
            return True
        return current == str(self._ocr_filled.get(field, "") or "").strip()

    def _snapshot_fields(self):
        """The form's current values (to tell whether a read changed anything)."""
        return (
            self._company_var.get().strip(),
            self.client_dropdown.entry.get().strip(),
            self.site_dropdown.entry.get().strip(),
            self._serial_var.get().strip(),
            tuple(self._selected_materials),
        )

    def _clear_ocr_values(self, fields=("company", "client", "site", "serial",
                                        "materials")) -> bool:
        """Drop everything OCR itself filled in (never the user's own edits).

        Used when a re-read comes back empty or fails: the values the rejected
        read had filled must not stay in the form looking like the user's data
        (and must not be saved by accident).
        """
        changed = False
        if "company" in fields and "company" in self._ocr_filled:
            self._ocr_filled.pop("company", None)
            self._ocr_company_override = None
            self._reload_company_options()  # back to the popup's default
            changed = True
        if "client" in fields and "client" in self._ocr_filled:
            self._ocr_filled.pop("client", None)
            self.client_dropdown.set("")
            self._client_var.set("")
            self._populate_sites("")
            changed = True
        if "site" in fields and "site" in self._ocr_filled:
            self._ocr_filled.pop("site", None)
            self.site_dropdown.set("")
            changed = True
        if "serial" in fields and "serial" in self._ocr_filled:
            self._ocr_filled.pop("serial", None)
            self._serial_var.set("")
            changed = True
        if "materials" in fields and self._ocr_materials:
            self._selected_materials = [
                m for m in self._selected_materials
                if m not in self._ocr_materials
            ]
            self._ocr_materials = set()
            try:
                self._render_material_chips()
            except Exception:
                pass
            changed = True
        if changed:
            self._refresh_preview()
        return changed

    def _apply_ocr_outcome(self, result) -> None:
        """Update the status line + fields once an OCR result is available."""
        # The read is over (success, empty or failure): stop refreshing the
        # "reading document…" counters so they can never overwrite the result.
        self._ocr_poll_done = True
        self._stop_ocr_progress()
        retrying = bool(getattr(self, "_ocr_retrying", False))
        self._ocr_retrying = False
        err = None
        if getattr(self, "ocr_pool", None) is not None:
            try:
                err = self.ocr_pool.get_error(self.file_path)
            except Exception:
                err = None
        if not result or not any(result.values()):
            # OCR could not read the document — most downloads still carry the
            # Delivery Note number in the file name, so back-fill the serial.
            # When the pool knows WHY it failed (e.g. a 500 gateway error),
            # say so and offer the retry button instead of a bare message.
            #
            # A FAILED RETRY additionally drops what the rejected read had
            # filled (only ever OCR's own values): keeping the wrong fields
            # would make the retry look like it did nothing.
            cleared = self._clear_ocr_values() if retrying else False
            # The file name fallback still applies (it is the download's own
            # name, not something a read produced) — the serial field is empty
            # again after a failed retry cleared it.
            from_name = (not self._serial_var.get().strip()
                         and self._apply_serial_from_filename())
            if err:
                message = self._short_ocr_error(err)
                if cleared:
                    message = message.replace(
                        " — click ↻ Retry OCR",
                        " — cleared the fields it had filled")
                self._set_ocr_status(message, _DANGER)
            elif cleared:
                self._set_ocr_status(
                    "OCR: could not read document — cleared the fields it "
                    "had filled"
                    + (" (serial from filename)" if from_name else ""), _DANGER)
            elif from_name:
                self._set_ocr_status(
                    "OCR: could not read document — serial from filename", _SUCCESS
                )
            else:
                self._set_ocr_status("OCR: could not read document")
            self._set_ocr_retry_visible(True)
            return
        changed = self._apply_ocr_result(result)
        # OCR missed the "Delivery Note No." field but the file name usually
        # carries the serial — back-fill it as a fallback (never clobbers).
        if not self._serial_var.get().strip() and self._apply_serial_from_filename():
            changed = True
        took = self._ocr_seconds()
        if changed:
            self._set_ocr_status(
                f"OCR: fields filled{took} — check before saving" if not retrying
                else f"OCR: re-read{took} — fields updated, check before saving",
                _SUCCESS,
            )
        elif getattr(self, "_ocr_kept_user_edits", None):
            # The read DID return values, but the fields it would touch hold
            # the user's own text. Say which ones instead of the old, opaque
            # "(fields already filled)" that made a retry look broken.
            self._set_ocr_status(
                f"OCR: done{took} — kept your "
                f"{' and '.join(self._ocr_kept_user_edits)}", _SUCCESS)
        else:
            self._set_ocr_status(f"OCR: done{took} (same result)", _SUCCESS)
        self._set_ocr_retry_visible(True)

    def _apply_ocr_result(self, result: Dict[str, str]) -> bool:
        """Pre-fill Company/Client/Site/Serial/materials from the OCR table.

        Values the USER typed are never clobbered — but values an EARLIER OCR
        read put in the form are replaced by a later read, so ↻ Retry really
        corrects a wrong answer instead of silently doing nothing (see
        _ocr_may_replace). The Serial field is filled independently of the
        dropdowns; the Company/Client/Site part is skipped when the user has
        started filling client or site themselves. Catalog entries are matched
        case-insensitively so the canonical spelling is used when one exists;
        unknown names stay typed as-is and flow through the normal Save / Add
        flows for the user to confirm.
        """
        before = self._snapshot_fields()
        log_bits: List[str] = []

        # Serial Number (free-text field) — digits only, 1-4 chars, already
        # normalised by the OCR parser.
        serial = (result.get("serial") or "").strip()
        if self._ocr_may_replace("serial", self._serial_var.get()):
            if serial:
                self._serial_var.set(serial)
                self._ocr_filled["serial"] = serial
            elif self._ocr_filled.pop("serial", None) is not None:
                # This read found no serial: drop the one WE filled before.
                self._serial_var.set("")

        # "Description of Goods" -> catalog materials: this read replaces the
        # materials the PREVIOUS read selected (the user's own picks and
        # deselections survive — see _apply_goods_materials).
        if self._apply_goods_materials(result.get("goods")):
            self._refresh_preview()

        company = (result.get("company") or "").strip()
        client = (result.get("client") or "").strip()
        site = (result.get("site") or "").strip()

        # Remember the RAW OCR values (incl. the serial) so the preview can
        # highlight exactly what the model read off the document — even when
        # the field ends up showing the mapped or near-matched name instead.
        self._ocr_highlight_terms = [
            v for v in (company, client, site, serial) if v
        ]

        # Fields the user has started filling themselves are left alone (the
        # serial and the materials above are still applied).
        self._ocr_kept_user_edits = [
            name for name, value in (
                ("client", self.client_dropdown.entry.get()),
                ("site", self.site_dropdown.entry.get()),
            ) if value.strip() and not self._ocr_may_replace(name, value)
        ]
        if self._ocr_kept_user_edits:
            log_bits.append(
                f"kept user {' and '.join(self._ocr_kept_user_edits)}")
            if self._ocr_materials:
                log_bits.append(f"materials {sorted(self._ocr_materials)}")
            self._set_preview_highlights()
            self._log_ocr_apply(log_bits)
            return self._snapshot_fields() != before

        if not (company or client or site):
            # Serial/materials-only result: the dropdown values an earlier read
            # filled are no longer supported by this read, so drop OUR values.
            self._clear_ocr_values(("company", "client", "site"))
            if self._ocr_materials:
                log_bits.append(f"materials {sorted(self._ocr_materials)}")
            self._set_preview_highlights()
            self._log_ocr_apply(log_bits)
            return self._snapshot_fields() != before

        # Name MAPPING (the 🗺 Map buttons): a name the user mapped to
        # another one is switched HERE, before any canonicalization, so OCR's
        # source name never reaches the fields — the mapped target is used.
        # ("...after getting ocr, the program auto maps and switches it to
        # the mapped one".)
        mapped_client = self.config.resolve_client(client)
        if mapped_client:
            client = mapped_client
        mapped_site = self.config.resolve_site(site)
        if mapped_site:
            site = mapped_site

        # Company (CTkOptionMenu): canonical catalog spelling when a
        # case-insensitive match exists, else keep the OCR text as-is (and
        # remember it so live-config refreshes don't revert it).
        if company:
            canonical = self._ci_canonical(self.config.companies, company)
            self._ocr_company_override = company if not canonical else None
            company_value = canonical if canonical else company
            self._company_var.set(company_value)
            self._ocr_filled["company"] = company_value
        elif self._ocr_filled.pop("company", None) is not None:
            self._reload_company_options()

        # Client + Site (searchable dropdowns): same canonical lookup;
        # unknown names stay typed and can be added at Save time.
        client_value = ""
        if client:
            # Near-match, same rule as sites: "Larsen and Toubro" resolves
            # to the catalog's "Larsen & Toubro", never a duplicate.
            canonical_client = self.config.find_near(
                list(self.config.clients.keys()), client
            )
            client_value = canonical_client if canonical_client else client
            self.client_dropdown.set(client_value)
            self._client_var.set(client_value)
            self._populate_sites(client_value)
            self._ocr_filled["client"] = client_value
            if client_value != client:
                log_bits.append(f"client {client!r} -> {client_value!r}")
        elif self._ocr_filled.pop("client", None) is not None:
            self.client_dropdown.set("")
            self._client_var.set("")
            self._populate_sites("")
        if site:
            # Site: resolve near-same spellings to the catalog name. NOTHING is
            # written to config.json here: a brand-new site is added only when
            # the file is actually SAVED (_submit), so wrong OCR on a popup
            # that gets skipped never pollutes the config.
            raw_site = site
            effective_client = self._client_var.get().strip()
            if effective_client:
                site = self._resolve_site_readonly(effective_client, site)
            self.site_dropdown.set(site)
            self._ocr_filled["site"] = site
            if site != raw_site:
                log_bits.append(f"site {raw_site!r} -> {site!r}")
        elif self._ocr_filled.pop("site", None) is not None:
            self.site_dropdown.set("")

        # Also mark the values FINALLY shown in the fields (post-mapping and
        # post-near-match) — the preview searches BOTH spellings, so the
        # mark appears whatever the document wrote ("Kalpataru Elitus Tower
        # 2" read, catalog "Kalpataru Elitus" shown; "Raymond Premium T-B"
        # read, "Raymond Premium" shown).
        for v in (client_value if client else "", site):
            if v and v not in self._ocr_highlight_terms:
                self._ocr_highlight_terms.append(v)

        if self._ocr_materials:
            log_bits.append(f"materials {sorted(self._ocr_materials)}")
        self._set_preview_highlights()
        self._refresh_preview()
        self._log_ocr_apply(log_bits)
        return self._snapshot_fields() != before

    def _set_preview_highlights(self) -> None:
        """Yellow-highlight the OCR-found values in the open preview."""
        if self._preview is None or not self._ocr_highlight_terms:
            return
        try:
            self._preview.set_highlight_terms(self._ocr_highlight_terms)
        except Exception:
            pass

    def _log_ocr_apply(self, bits: List[str]) -> None:
        """Log what the app DID with the read (the OCR log line prints what the
        model returned): catalog mapping, near-match, materials, user edits
        kept. A wrong field can then be traced to the read or to the matching
        without guessing."""
        if not bits:
            return
        try:
            print(f"[filepicker] OCR applied for {self.file_path.name}: "
                  + "; ".join(bits))
        except Exception:
            pass

    def _apply_serial_from_filename(self) -> bool:
        """Fill the serial field from the download file name (fallback).

        Most delivery notes carry their "Delivery Note No." in the file name
        (e.g. ``RS-DC-26-27-6.pdf`` -> ``6``). Only applied while the serial
        field is still empty. Returns True when the field was filled.
        """
        if self._serial_var.get().strip():
            return False
        try:
            from ocr import serial_from_filename
            serial = serial_from_filename(self.file_path)
        except Exception:
            return False
        if not serial:
            return False
        self._serial_var.set(serial)
        return True

    @staticmethod
    def _ci_canonical(values: List[str], name: str) -> Optional[str]:
        """The catalog spelling matching *name* case-insensitively, if any."""
        lowered = name.strip().lower()
        for value in values:
            if str(value).strip().lower() == lowered:
                return str(value)
        return None

    def _resolve_site_readonly(self, client: str, site: str) -> str:
        """Canonicalize *site* WITHOUT touching the config.

        A trailing dashed unit designator is dropped first ("Wrong Site
        T-9/10" -> "Wrong Site"), then near-same spellings resolve to the
        existing catalog name so the field shows the canonical site; a
        genuinely unknown site is returned as typed. NOTHING is written to
        config.json here — the site is only added when the file is actually
        SAVED (``_submit``), because OCR can be wrong and a half-filled popup
        that is skipped must never leave a bogus site behind in the config.
        """
        site = (site or "").strip()
        client = (client or "").strip()
        if not site:
            return site
        try:
            if self.config.site_is_designator_only(site):
                # "Tower-A" / "T-9/10" / "Tower -C" — no place name at all.
                # Leave the field EMPTY (the save validation then asks the
                # user to pick the real site) instead of inventing a site
                # called "Tower-A" in the config.
                print(f"[filepicker] OCR site '{site}' is only a unit "
                      "designator — leaving Site empty for you to choose")
                return ""
        except Exception:
            pass
        try:
            site = self.config.site_display_name(site)
        except Exception:
            pass
        if not client:
            return site
        try:
            canonical = self.config.find_near_site(self.config.sites_for(client), site)
            if canonical is not None:
                return str(canonical)
        except Exception as exc:
            print(f"[filepicker] site lookup error: {exc}")
        return site

    def _ensure_site_in_config(self, client: str, site: str) -> str:
        """Canonicalize *site* against the catalog; add + push brand-new sites.

        Called only from the SAVE path (``_submit``). Returns the site name
        to use:

        - the existing catalog spelling when *site* is the same place as one
          of the client's sites (near-match: case/spacing/articles/1-letter
          variants/tolerated extra word) — nothing is added;
        - otherwise *site* is added to the config for *client* (which is
          created if missing) and pushed back to GitHub, so the site appears
          in the dropdown of this and every later popup.

        The dropdown values are refreshed so the returned name is selectable.
        """
        site = (site or "").strip()
        client = (client or "").strip()
        if not site or not client:
            return site
        try:
            canonical = self.config.find_near_site(self.config.sites_for(client), site)
            if canonical is not None:
                return str(canonical)
        except Exception:
            pass
        try:
            if self.config.site_is_designator_only(site):
                # "Tower B" / "Wing C" / "T-9/10" — no place name. Never write
                # it to the config; keep the typed value so the field still
                # shows what the user entered (they can pick the real site).
                print(f"[filepicker] site '{site}' is only a unit designator "
                      "— not adding it to the config")
                return site
        except Exception:
            pass
        try:
            effective = self.config.add_site(client, site)
        except Exception as exc:
            print(f"[filepicker] could not add site '{site}': {exc}")
            return site
        # New site: refresh the dropdown so it is offered right away.
        try:
            values = self.config.sites_for(client) + [ADD_NEW_SITE_OPTION]
            self.site_dropdown.configure(values=values)
        except Exception:
            pass
        return effective

    # ------------------------------------------------------------------
    # Preview + submit
    # ------------------------------------------------------------------
    def _refresh_preview(self) -> None:
        """Update the live filename preview as the user edits fields."""
        import filename as fn

        ext = self.file_path.suffix.lstrip(".") or "pdf"
        try:
            name = fn.build_filename(
                company=self._company_var.get(),
                doc_type=self._doc_type_var.get(),
                site_name=self._current_site(),
                selected_materials=self._selected_materials,
                materials_map=self._materials_map,
                serial=self._serial_var.get(),
                extension=ext,
            )
        except Exception:
            name = ""
        self.preview_label.configure(text=f"Preview: {name}" if name else "")

    def _submit(self) -> None:
        status = "Received" if self._received_var.get() else "Submitted"
        client = self.client_dropdown.get().strip()
        site = self._current_site().strip()
        # Validation — client & site must be chosen (no default anymore)
        if not client or client == ADD_NEW_CLIENT_OPTION:
            self.preview_label.configure(text="⚠ Please select a Client", text_color=_DANGER)
            try:
                self.client_dropdown.entry.focus_set()
            except Exception:
                pass
            return
        if not site or site == ADD_NEW_SITE_OPTION:
            self.preview_label.configure(text="⚠ Please select a Site", text_color=_DANGER)
            try:
                self.site_dropdown.entry.focus_set()
            except Exception:
                pass
            return
        # A bare unit designator ("Tower B", "Wing C", "T-9/10") is never a
        # site — unless the catalog genuinely has one, saving is stopped so
        # neither the config nor the folder path gets "Tower B" (the user
        # picks the real site instead).
        try:
            known_site = self.config.find_near_site(
                self.config.sites_for(client), site)
        except Exception:
            known_site = None
        if not known_site:
            try:
                if self.config.site_is_designator_only(site):
                    self.preview_label.configure(
                        text="⚠ That is only a tower/block/wing — please select the real Site",
                        text_color=_DANGER)
                    try:
                        self.site_dropdown.entry.focus_set()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        # Name MAPPING also applies at save time: a mapped name typed or selected
        # manually lands in the target's folder, exactly like OCR would.
        try:
            mapped_client = self.config.resolve_client(client)
            if mapped_client and mapped_client != client:
                client = mapped_client
                self.client_dropdown.set(client)
        except Exception:
            pass
        try:
            mapped_site = self.config.resolve_site(site)
            if mapped_site and mapped_site != site:
                site = mapped_site
        except Exception:
            pass
        # Every saved site lands in the config: near-same spellings resolve to
        # the existing catalog name, and genuinely new sites (typed or from
        # OCR) are added + pushed to GitHub so the next popup offers them.
        # Same-place CLIENT names resolve to the catalog spelling too, so the
        # folder path is canonical ("Larsen and Toubro" -> "Larsen & Toubro").
        try:
            canonical_client = self.config.find_near(
                list(self.config.clients.keys()), client
            )
            if canonical_client and canonical_client != client:
                client = canonical_client
                self.client_dropdown.set(client)
        except Exception:
            pass
        # Cross-client site check (0.6.28): is *site* already the same place as
        # a site of ANOTHER client? This is a WARNING only — the file is never
        # shifted to the other client and nothing is ever merged automatically.
        # The user explicitly chooses one of:
        #   continue  -> save under THIS client as chosen (warning only);
        #   transfer  -> move ALL sites of the conflicting client(s) into THIS
        #                client, then save (explicit "move all sites from
        #                other client to present one which we save");
        #   cancel    -> abort; the popup stays open so the user can pick the
        #                other client themselves.
        try:
            conflicts = self.config.find_similar_site_other_client(client, site)
        except Exception:
            conflicts = []
        if conflicts:
            try:
                choice = ask_cross_client_site(
                    self.window, client, site, conflicts)
            except Exception:
                choice = "continue"
            if choice == "cancel":
                return
            if choice == "transfer":
                for other, _other_site in conflicts:
                    try:
                        self.config.move_client_sites(other, client)
                    except Exception as exc:
                        print(f"[filepicker] could not move sites from "
                              f"'{other}': {exc}")
                # Refresh the dropdowns so the merged catalog is live (and the
                # removed client disappears from the list).
                try:
                    self.client_dropdown.configure(
                        values=list(self.config.clients.keys())
                        + [ADD_NEW_CLIENT_OPTION])
                    self.client_dropdown.set(client)
                    self._client_var.set(client)
                    self._populate_sites(client)
                except Exception:
                    pass
        site = self._ensure_site_in_config(client, site)
        if site != self.site_dropdown.get():
            try:
                self.site_dropdown.set(site)
            except Exception:
                pass
            self._refresh_preview()
        payload = {
            "file_path": self.file_path,
            "company": self._company_var.get(),
            "client": client,
            "site": site,
            "doc_type": self._doc_type_var.get(),
            "materials": list(self._selected_materials),
            "serial": self._serial_var.get(),
            "status": status,
        }
        self._release()
        self.on_submit(payload)

    def _current_site(self) -> str:
        """The site to use for the file.

        Uses whatever the user typed/selected in the site box (the dropdown's
        live value), so the folder is created for the site the user actually
        entered — never a fallback to the first item or the add-new sentinel.
        """
        site = self.site_dropdown.get()
        if site == ADD_NEW_SITE_OPTION:
            return self.client_dropdown.get() or site
        return site

    def _skip(self) -> None:
        self._release()
        self.on_skip()

    def _skip_all(self) -> None:
        """Dismiss this popup AND every queued popup, deleting the files.

        The popup (and its preview, which holds the file open) is fully
        released FIRST — Windows cannot delete files that another process
        has open. Only then is the controller told to clear the queue and
        remove the files from the watch folder.
        """
        self._release()
        try:
            if self.on_skip_all is not None:
                self.on_skip_all()
        except Exception as exc:
            print(f"[filepicker] skip-all error: {exc}")

    def _release(self) -> None:
        # Stop live config polling first so no after() fires on a destroyed window.
        try:
            self._stop_config_poll()
        except Exception:
            pass
        # Same for the OCR status tick ("reading document… (N read together)").
        try:
            self._ocr_poll_done = True
            self._stop_ocr_progress()
        except Exception:
            pass
        # Close the preview first so it releases the file handle; otherwise the
        # source file stays locked on Windows and can't be deleted afterwards.
        if self._preview is not None:
            try:
                self._preview.destroy()
            except Exception:
                pass
            self._preview = None
        # Close any dropdown popups
        try:
            self.client_dropdown._close()
            self.site_dropdown._close()
        except Exception:
            pass
        # Release the global Alt-block hook (keys return to normal for every
        # other program once no FilePicker window is on screen). Ref-counted:
        # removing THIS window's hwnd never disables blocking while another
        # FilePicker window (e.g. the duplicate dialog) is still open.
        if getattr(self, "_alt_block_active", False):
            try:
                import altblock
                altblock.remove(self.window.winfo_id())
            except Exception:
                pass
            self._alt_block_active = False
        self.window.destroy()

    def show(self) -> None:
        """Wait for the modal popup to be dismissed (blocking).

        Uses wait_window() instead of a nested mainloop(): a nested mainloop()
        on a Toplevel never returns once the window is destroyed, which would
        wedge the controller's popup loop and stop the next queued file from
        ever being shown.
        """
        self.window.wait_window()
