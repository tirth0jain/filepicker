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

import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

import filename as fn
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


def _duplicate_dialog_ui(root, filename: str, existing_path: Path) -> tuple:
    """Build the "file already exists" question dialog.

    Returns ``(dialog, callback)`` where ``callback["value"]`` is set to
    ``"skip"`` or ``"replace"`` when a button is pressed (the dialog is
    destroyed with it). Closing the window counts as "skip".
    """
    dialog = ctk.CTkToplevel(root)
    dialog.title("File already exists")
    dialog.configure(fg_color=_BG)
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)

    # Center over the popup/screen, like every other FilePicker window.
    try:
        sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        dialog.geometry(f"500x240+{max((sw - 500) // 2, 0)}+{max((sh - 240) // 3, 0)}")
    except tk.TclError:
        pass

    callback = {"value": None}

    def choose(choice: str) -> None:
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
             "The new download would be saved with the same name.\n"
             "What should FilePicker do?\n\n"
             "Ctrl+S = Replace  •  Ctrl+Delete = Skip",
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED, justify="left",
        wraplength=460,
    ).pack(anchor="w", padx=18, pady=(0, 12))

    btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_row.pack(fill="x", padx=18, pady=(0, 16))
    skip_btn = ctk.CTkButton(
        btn_row, text="Skip New File", command=lambda: choose("skip"),
        fg_color=_BG_FIELD, hover_color="#33334a", height=38,
        font=ctk.CTkFont(size=13), text_color=_TEXT,
    )
    skip_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
    replace_btn = ctk.CTkButton(
        btn_row, text="Replace Old with New", command=lambda: choose("replace"),
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
    try:
        skip_btn.focus_set()
    except tk.TclError:
        pass
    return dialog, callback


def ask_duplicate_action(root, filename: str, existing_path: Path) -> str:
    """Ask what to do when the output filename already exists in sorted.

    Blocks (modal) until the user answers. Returns ``"skip"`` — keep the old
    file and leave the new download in the watch folder — or ``"replace"`` —
    overwrite the old file with the new one. Closing the dialog counts as
    ``"skip"`` (the safe default).
    """
    dialog, callback = _duplicate_dialog_ui(root, filename, existing_path)
    dialog.wait_window()
    return callback["value"] or "skip"

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
        # Don't reopen immediately after Enter (Return) — _on_return already closed.
        if _e is not None and getattr(_e, "keysym", None) == "Return":
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
        self.kind = kind  # "client" | "site"
        self.config = config
        self._on_changed = on_changed
        self._rows: List[tuple] = []  # [(source, target), ...] in the list

        self.win = ctk.CTkToplevel(parent)
        self.win.title("Map Client" if kind == "client" else "Map Site")
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

        what = "client" if kind == "client" else "site"
        ctk.CTkLabel(
            body,
            text=f"When OCR (or this popup) reads the name on the left, "
                 f"FilePicker switches it to the name on the right. Future "
                 f"downloads of the same {what} are filed under the mapped "
                 f"name automatically.",
            font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED, justify="left",
            wraplength=520,
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(body, text="Document shows / OCR reads:",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.source_dd = SearchableDropdown(body, values=list(names),
                                            on_change=None)
        self.source_dd.entry.configure(
            placeholder_text="Search or type the name the document has…",
        )
        self.source_dd.set(current)

        ctk.CTkLabel(body, text="Map to:",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(6, 2))
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
        return self.config.site_aliases

    def _set_alias(self, source: str, target: str) -> bool:
        if self.kind == "client":
            return self.config.set_client_alias(source, target)
        return self.config.set_site_alias(source, target)

    def _remove_alias(self, source: str) -> bool:
        if self.kind == "client":
            return self.config.remove_client_alias(source)
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
        # Company name placed by OCR that is NOT in the catalog — kept across
        # live-config refreshes until the user picks a menu value.
        self._ocr_company_override: Optional[str] = None
        # Raw Company/Client/Site values the OCR returned (before any
        # canonicalization) — shown highlighted in yellow in the preview.
        self._ocr_highlight_terms: List[str] = []

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

        # Keep the popup on top of everything.
        self.window.attributes("-topmost", True)
        self.window.lift()
        self.window.focus_force()

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
        self._win_h = max(min(820, _screen_h - 20), 700)
        _width = 560
        if self.config.preview_open_by_default \
                and self.file_path.suffix.lower() in _SUPPORTED_PREVIEW_EXTS:
            _width = 1180
        self.window.geometry(f"{_width}x{self._win_h}+{max((_screen_w - _width) // 2, 0)}+0")
        self.window.configure(fg_color=_BG)
        self.window.resizable(True, True)  # height adjustable
        self.window.minsize(560, 700)
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

        # -- Target file banner -----------------------------------------
        self._banner = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=10)
        self._banner.pack(fill="x", pady=(0, 6))

        banner_header = ctk.CTkFrame(self._banner, fg_color="transparent")
        banner_header.pack(fill="x", padx=12, pady=(6, 0))
        self._banner_name = ctk.CTkLabel(
            banner_header, text="", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT, wraplength=380, justify="left",
        )
        self._banner_name.pack(side="left", anchor="w")
        # Minimize: the popup (and its modal grab) must not block the user
        # from going elsewhere — the window minimizes to the taskbar and is
        # restored from there. Title-bar minimize works too.
        self.minimize_btn = ctk.CTkButton(
            banner_header, text="—", width=40, height=28,
            fg_color=_BG_FIELD, hover_color="#33334a",
            text_color=_TEXT_MUTED, command=self._minimize_popup,
        )
        self.minimize_btn.pack(side="right", anchor="e", padx=(0, 6))
        self.preview_btn = ctk.CTkButton(
            banner_header, text="👁 Preview", width=96, height=28,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._toggle_preview,
        )
        self.preview_btn.pack(side="right", anchor="e")

        self._banner_size = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        )
        self._banner_size.pack(anchor="w", padx=12, pady=(0, 6))

        # OCR status line — shows OCR progress, or the exact reason OCR is off
        # (disabled in config / no API key), never silently nothing. The
        # "↻ Retry OCR" button on the right appears once OCR finished (filled
        # or failed) and re-runs the vision call for this file.
        self._ocr_row = ctk.CTkFrame(f, fg_color="transparent")
        self._ocr_row.pack(fill="x", pady=(0, 2))
        self._ocr_label = ctk.CTkLabel(
            self._ocr_row, text="", font=ctk.CTkFont(size=11),
            text_color=_TEXT_MUTED, anchor="w",
        )
        self._ocr_label.pack(side="left", fill="x", expand=True)
        self.retry_ocr_btn = ctk.CTkButton(
            self._ocr_row, text="↻ Retry OCR", width=92, height=22,
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT,
            font=ctk.CTkFont(size=11), command=self._retry_ocr,
        )
        self.retry_ocr_btn.pack(side="right")
        self.retry_ocr_btn.pack_forget()  # shown only after OCR finished/failed

        # -- Company ----------------------------------------------------
        ctk.CTkLabel(f, text="Company", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 1))
        self.company_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._company_var,
            command=self._on_company_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.company_combo.pack(fill="x", pady=(0, 6))

        # -- Client -----------------------------------------------------
        client_header = ctk.CTkFrame(f, fg_color="transparent")
        client_header.pack(fill="x", pady=(0, 1))
        ctk.CTkLabel(client_header, text="Client",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(side="left")
        # 🗺 Map: alias a client name the document shows/OCR reads to another
        # client — future popups auto-switch the mapped name to its target.
        self.client_map_btn = ctk.CTkButton(
            client_header, text="🗺 Map", width=72, height=22,
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: self._open_mapping_dialog("client"),
        )
        self.client_map_btn.pack(side="right")
        self.client_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_client_change,
        )
        self.client_dropdown.entry.configure(
            placeholder_text="Search client…",
        )

        # -- Site -------------------------------------------------------
        site_header = ctk.CTkFrame(f, fg_color="transparent")
        site_header.pack(fill="x", pady=(0, 1))
        ctk.CTkLabel(site_header, text="Site",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(side="left")
        self.site_map_btn = ctk.CTkButton(
            site_header, text="🗺 Map", width=72, height=22,
            fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
            font=ctk.CTkFont(size=11),
            command=lambda: self._open_mapping_dialog("site"),
        )
        self.site_map_btn.pack(side="right")
        self.site_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_site_change,
        )
        self.site_dropdown.entry.configure(
            placeholder_text="Search site…",
        )

        # -- Document type ----------------------------------------------
        ctk.CTkLabel(f, text="Document Type", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 1))
        self.doc_type_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._doc_type_var,
            command=lambda _d: self._refresh_preview(),
            fg_color=_BG_FIELD, button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.doc_type_combo.pack(fill="x", pady=(0, 6))

        # -- Materials (multi-select) -----------------------------------
        ctk.CTkLabel(f, text="Material (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 1))
        self.material_frame = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=8)
        self.material_frame.pack(fill="x", pady=(0, 6))
        # Scrollable chip area (plain Canvas + scrollbar — the same pattern as
        # viewer.py): the chip rows pack into _material_inner and scroll when
        # they exceed the visible height (capped at _MATERIAL_ROWS_VISIBLE).
        self._material_canvas = tk.Canvas(
            self.material_frame, bg=_BG_SECONDARY, highlightthickness=0, bd=0,
            height=_MATERIAL_ROW_PITCH * _MATERIAL_ROWS_VISIBLE,
        )
        self._material_vsb = ttk.Scrollbar(
            self.material_frame, orient="vertical", command=self._material_canvas.yview,
        )
        self._material_canvas.configure(yscrollcommand=self._material_vsb.set)
        self._material_canvas.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        self._material_vsb.pack(side="right", fill="y", padx=(0, 6), pady=6)
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
        self._render_material_chips()

        # -- Serial number ----------------------------------------------
        ctk.CTkLabel(f, text="Serial Number",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 1))
        self.serial_entry = ctk.CTkEntry(
            f, textvariable=self._serial_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT,
        )
        self.serial_entry.pack(fill="x", pady=(0, 6))

        # -- Received copy checkbox -------------------------------------
        self.received_check = ctk.CTkCheckBox(
            f, text="Received Copy (unchecked = Submitted)",
            variable=self._received_var, fg_color=_ACCENT,
            hover_color=_ACCENT, text_color=_TEXT,
        )
        self.received_check.pack(anchor="w", pady=(0, 6))

        # -- Buttons ----------------------------------------------------
        btn_row = ctk.CTkFrame(f, fg_color=_BG)
        btn_row.pack(fill="x", pady=(2, 0))

        self.save_btn = ctk.CTkButton(
            btn_row, text="Save & Organize", command=self._submit,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, height=40,
            font=ctk.CTkFont(size=14, weight="bold"), text_color="#ffffff",
        )
        self.save_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.skip_btn = ctk.CTkButton(
            btn_row, text="Skip / Keep Original", command=self._skip,
            fg_color=_BG_FIELD, hover_color="#33334a", height=40,
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
                fg_color="#3a2b2b", hover_color="#4a3535", width=140, height=40,
                font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
            )
            self.skip_all_btn.pack(side="left", fill="y", padx=(8, 0))

        # -- Live preview ----------------------------------------------
        self.preview_label = ctk.CTkLabel(
            f, text="", font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
            wraplength=500, justify="left",
        )
        self.preview_label.pack(fill="x", pady=(4, 0))
        self._refresh_preview()

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
                row_frame, text=text, width=0, height=28,
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

        # Cap the visible chip area at _MATERIAL_ROWS_VISIBLE rows; extra rows
        # scroll (mouse wheel over the panel). Shrinks to fit small catalogs.
        # Height = pitch * rows: a 34px pitch (28px chip + 6px gap) means the
        # full last row is visible without extra top padding.
        self._material_canvas.configure(
            height=_MATERIAL_ROW_PITCH * min(rows, _MATERIAL_ROWS_VISIBLE)
        )

    def _toggle_material(self, name: str) -> None:
        if name in self._selected_materials:
            self._selected_materials.remove(name)
        else:
            self._selected_materials.append(name)
        self._render_material_chips()
        self._refresh_preview()

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
        """Open the Map <Client|Site> editor for *kind*.

        The dialog lists the existing mappings (searchable) and lets the
        user map the name the current document shows (pre-filled from the
        field) to the name to use instead. Future OCR reads of the mapped
        source switch to the target automatically.
        """
        if kind == "client":
            names = list(self.config.clients.keys())
            current = self.client_dropdown.get().strip()
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
                    site = self._ensure_site_in_config(client, mapped) \
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
    # OCR auto-fill (OpenCode Go "DeepSeek V4 Flash Vision Exp")
    # ------------------------------------------------------------------
    def _set_ocr_status(self, text: str, color: str = _TEXT_MUTED) -> None:
        label = getattr(self, "_ocr_label", None)
        if label is None:
            return
        try:
            label.configure(text=text, text_color=color)
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

        The pool forgets the failed (or stale) cached result and makes a
        fresh vision call; the outcome is applied exactly like the first
        read (fields the user already filled are never clobbered).
        """
        pool = getattr(self, "ocr_pool", None)
        if pool is None or not pool.available:
            return
        try:
            self.retry_ocr_btn.configure(state="disabled")
        except Exception:
            pass
        self._set_ocr_status("OCR: retrying…", _ACCENT)

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

        pool.retry(self.file_path, on_done)

    def _start_ocr(self) -> None:
        """Consume the background OCR result for this file (if any).

        OCR of every completed download is kicked off eagerly by the
        controller's OcrPool (bounded to 10 concurrent vision calls), so by
        the time a popup opens the result is usually already cached. If it is
        still in flight we subscribe to its completion; results are applied
        on the UI thread and never clobber anything the user already typed.
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
        if cached is not None:
            self._apply_ocr_outcome(cached)
            return

        self._set_ocr_status("OCR: reading document…", _ACCENT)

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

    def _apply_ocr_outcome(self, result) -> None:
        """Update the status line + fields once an OCR result is available."""
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
            if err:
                self._set_ocr_status(self._short_ocr_error(err), _DANGER)
            elif self._apply_serial_from_filename():
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
        self._set_ocr_status(
            "OCR: fields filled — check before saving" if changed
            else "OCR: done (fields already filled)",
            _SUCCESS,
        )
        self._set_ocr_retry_visible(True)

    def _apply_ocr_result(self, result: Dict[str, str]) -> bool:
        """Pre-fill Company/Client/Site/Serial from the OCR table.

        Never clobbers fields the user already typed. The Serial field is
        filled independently of the dropdowns (as long as it is still empty);
        the Company/Client/Site part is skipped if the user already started
        filling client or site. Catalog entries are matched case-insensitively
        so the canonical spelling is used when one exists; unknown names stay
        typed as-is and flow through the normal Save / Add flows for the user
        to confirm.
        """
        changed = False

        # Serial Number (free-text field) — digits only, 1-4 chars, already
        # normalised by the OCR parser.
        serial = (result.get("serial") or "").strip()
        if serial and not self._serial_var.get().strip():
            self._serial_var.set(serial)
            changed = True

        # If the user already started filling client/site, leave the dropdown
        # fields alone (the serial above is still applied, though).
        if self.client_dropdown.entry.get().strip() or self.site_dropdown.entry.get().strip():
            return changed

        company = (result.get("company") or "").strip()
        client = (result.get("client") or "").strip()
        site = (result.get("site") or "").strip()
        if not (company or client or site):
            return changed

        # Remember the raw OCR values so the preview can highlight exactly
        # what the model read off the document (in yellow).
        self._ocr_highlight_terms = [v for v in (company, client, site) if v]

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
            self._company_var.set(canonical if canonical else company)

        # Client + Site (searchable dropdowns): same canonical lookup;
        # unknown names stay typed and can be added at Save time.
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
        if site:
            # Site: resolve near-same spellings to the catalog name (the AI
            # may still return "sital baug" when the config has "Sital Baug"),
            # and when the site is genuinely new, add it to the config + push
            # to GitHub right away so the next popup offers it.
            effective_client = self._client_var.get().strip()
            if effective_client:
                site = self._ensure_site_in_config(effective_client, site)
            self.site_dropdown.set(site)

        self._refresh_preview()
        # Yellow-highlight the OCR-found values in the open preview (the
        # viewer re-renders the current page with cheap text-search boxes).
        if self._preview is not None:
            try:
                self._preview.set_highlight_terms(self._ocr_highlight_terms)
            except Exception:
                pass
        return True

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

    def _ensure_site_in_config(self, client: str, site: str) -> str:
        """Canonicalize *site* against the catalog; add + push brand-new sites.

        Returns the site name to use:

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
        # other program once no popup is on screen).
        if getattr(self, "_alt_block_active", False):
            try:
                import altblock
                altblock.remove()
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
