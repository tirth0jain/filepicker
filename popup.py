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

# How often the open popup polls GitHub for live config (ms).
_CONFIG_POLL_MS = 30_000

# Material chips panel: at most this many rows of chips are visible at once;
# any extra rows scroll inside the panel (a large catalog must never push the
# Serial number / Save buttons out of the fixed-height popup window).
_MATERIAL_ROWS_VISIBLE = 3
_MATERIAL_ROW_PITCH = 36   # 28px chip + 8px bottom padding per row
_MATERIAL_TOP_PAD = 8

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
        self.pack(fill="x", pady=(0, 8))

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


class FilePickerPopup:
    """Modal dialog that gathers metadata and hands it to a callback."""

    def __init__(
        self,
        config: ConfigManager,
        file_path: Path,
        on_submit: Callable[[dict], None],
        on_skip: Callable[[], None],
        ocr_pool=None,
    ) -> None:
        self.config = config
        self.file_path = Path(file_path)
        self.on_submit = on_submit
        self.on_skip = on_skip
        self.ocr_pool = ocr_pool  # OcrPool (eager background OCR) or None

        # Internal UI state.
        self._company_var = tk.StringVar()
        self._client_var = tk.StringVar()
        self._doc_type_var = tk.StringVar(value="DC")
        self._serial_var = tk.StringVar()
        self._received_var = tk.BooleanVar(value=True)
        self._selected_materials: List[str] = []
        # Company name placed by OCR that is NOT in the catalog — kept across
        # live-config refreshes until the user picks a menu value.
        self._ocr_company_override: Optional[str] = None

        # Material name -> shortcode mapping loaded once.
        self._materials_map: Dict[str, str] = {}

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._build_window()
        self._build_ui()
        self._reload_config_state()
        self._set_banner()

        # Live-update the preview when the serial or checkbox changes.
        self._serial_var.trace_add("write", lambda *_: self._refresh_preview())
        self._received_var.trace_add("write", lambda *_: self._refresh_preview())

        # Keep the popup on top of everything.
        self.window.attributes("-topmost", True)
        self.window.lift()
        self.window.focus_force()

        # Live config: refresh while open if someone pushes a new clients/sites list.
        self._config_poll_after = None
        self._start_config_poll()

        # OCR auto-fill (only when enabled in config.json).
        if self.config.enable_ocr:
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
            self.window.geometry(f"560x{self._win_h}")
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
            self.window.geometry(f"560x{self._win_h}")
            print(f"[filepicker] preview error: {exc}")
            return
        self.window.geometry(f"1180x{self._win_h}")
        self.preview_btn.configure(text="✕ Close Preview")

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
        _screen_w = self.window.winfo_screenwidth()
        _screen_h = self.window.winfo_screenheight()
        self._win_h = max(min(820, _screen_h - 20), 700)
        self.window.geometry(f"560x{self._win_h}+{max((_screen_w - 560) // 2, 0)}+0")
        self.window.configure(fg_color=_BG)
        self.window.resizable(True, True)  # height adjustable
        self.window.minsize(560, 700)
        self.window.protocol("WM_DELETE_WINDOW", self._skip)

        # Modal behaviour: grab all input until dismissed.
        self.window.transient()
        self.window.grab_set()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        container = ctk.CTkFrame(self.window, fg_color=_BG, corner_radius=0)
        container.pack(fill="both", expand=True, padx=14, pady=12)

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
        self._banner.pack(fill="x", pady=(0, 10))

        banner_header = ctk.CTkFrame(self._banner, fg_color="transparent")
        banner_header.pack(fill="x", padx=12, pady=(8, 0))
        self._banner_name = ctk.CTkLabel(
            banner_header, text="", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT, wraplength=380, justify="left",
        )
        self._banner_name.pack(side="left", anchor="w")
        self.preview_btn = ctk.CTkButton(
            banner_header, text="👁 Preview", width=96, height=28,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._toggle_preview,
        )
        self.preview_btn.pack(side="right", anchor="e")

        self._banner_size = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        )
        self._banner_size.pack(anchor="w", padx=12, pady=(0, 8))

        # OCR status line — stays empty (invisible) unless enable_ocr is on.
        self._ocr_label = ctk.CTkLabel(
            f, text="", font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED, anchor="w",
        )
        self._ocr_label.pack(fill="x", pady=(0, 4))

        # -- Company ----------------------------------------------------
        ctk.CTkLabel(f, text="Company", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.company_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._company_var,
            command=self._on_company_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.company_combo.pack(fill="x", pady=(0, 8))

        # -- Client -----------------------------------------------------
        ctk.CTkLabel(f, text="Client", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.client_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_client_change,
        )
        self.client_dropdown.entry.configure(
            placeholder_text="Search client…",
        )

        # -- Site -------------------------------------------------------
        ctk.CTkLabel(f, text="Site", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.site_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_site_change,
        )
        self.site_dropdown.entry.configure(
            placeholder_text="Search site…",
        )

        # -- Document type ----------------------------------------------
        ctk.CTkLabel(f, text="Document Type", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.doc_type_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._doc_type_var,
            command=lambda _d: self._refresh_preview(),
            fg_color=_BG_FIELD, button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.doc_type_combo.pack(fill="x", pady=(0, 8))

        # -- Materials (multi-select) -----------------------------------
        ctk.CTkLabel(f, text="Material (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.material_frame = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=8)
        self.material_frame.pack(fill="x", pady=(0, 8))
        # Scrollable chip area (plain Canvas + scrollbar — the same pattern as
        # viewer.py): the chip rows pack into _material_inner and scroll when
        # they exceed the visible height (capped at _MATERIAL_ROWS_VISIBLE).
        self._material_canvas = tk.Canvas(
            self.material_frame, bg=_BG_SECONDARY, highlightthickness=0, bd=0,
            height=_MATERIAL_ROW_PITCH * _MATERIAL_ROWS_VISIBLE - _MATERIAL_TOP_PAD,
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
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 2))
        self.serial_entry = ctk.CTkEntry(
            f, textvariable=self._serial_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT,
        )
        self.serial_entry.pack(fill="x", pady=(0, 8))

        # -- Received copy checkbox -------------------------------------
        self.received_check = ctk.CTkCheckBox(
            f, text="Received Copy (unchecked = Submitted)",
            variable=self._received_var, fg_color=_ACCENT,
            hover_color=_ACCENT, text_color=_TEXT,
        )
        self.received_check.pack(anchor="w", pady=(0, 10))

        # -- Buttons ----------------------------------------------------
        btn_row = ctk.CTkFrame(f, fg_color=_BG)
        btn_row.pack(fill="x", pady=(4, 0))

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

        # -- Live preview ----------------------------------------------
        self.preview_label = ctk.CTkLabel(
            f, text="", font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
            wraplength=500, justify="left",
        )
        self.preview_label.pack(fill="x", pady=(6, 0))
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Config-driven state
    # ------------------------------------------------------------------
    def _reload_config_state(self) -> None:
        data = self.config.load()
        companies = data.get("companies", [])
        clients = data.get("clients", {})
        self._materials_map = dict(data.get("materials", {}))

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

        Called every :data:`_CONFIG_POLL_MS` and also when the controller detects
        a live GitHub change while the popup is open. Returns True if the UI
        changed.
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
        new_materials = dict(data.get("materials", {}))
        if new_materials != self._materials_map:
            self._materials_map = new_materials
            # Keep only selected materials that still exist
            self._selected_materials = [m for m in self._selected_materials if m in new_materials]
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
        """Begin polling GitHub for live config while the popup is open."""
        self._config_poll_after = None
        if not self.config.enable_live_config:
            return
        # Immediate fetch shortly after open so the popup never shows stale data
        # for 30s — then every _CONFIG_POLL_MS thereafter.
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
        """Background fetch → apply → refresh (never blocks the UI)."""
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
            finally:
                try:
                    if self.window.winfo_exists():
                        self._config_poll_after = self.window.after(_CONFIG_POLL_MS, self._poll_config)
                except tk.TclError:
                    pass

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

        for name in self._materials_map:
            selected = name in self._selected_materials
            chip = place_chip(
                f"{name} ({self._materials_map[name]})",
                _ACCENT if selected else _BG_FIELD,
                _ACCENT_HOVER if selected else "#33334a",
                "#ffffff" if selected else _TEXT,
                lambda n=name: self._toggle_material(n),
            )
            self._material_chips[name] = chip

        place_chip(
            "+ Add Material", _BG_FIELD, "#33334a", _ACCENT,
            self._prompt_add_material,
        )

        # Cap the visible chip area at _MATERIAL_ROWS_VISIBLE rows; extra rows
        # scroll (mouse wheel over the panel). Shrinks to fit small catalogs.
        self._material_canvas.configure(
            height=_MATERIAL_ROW_PITCH * min(rows, _MATERIAL_ROWS_VISIBLE)
            - _MATERIAL_TOP_PAD
        )

    def _toggle_material(self, name: str) -> None:
        if name in self._selected_materials:
            self._selected_materials.remove(name)
        else:
            self._selected_materials.append(name)
        self._render_material_chips()
        self._refresh_preview()

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
        # Auto-derive a shortcode from the first letters if not provided.
        shortcode = self._derive_shortcode(name)
        self.config.add_material(name, shortcode)
        self._materials_map = self.config.materials
        if name not in self._selected_materials:
            self._selected_materials.append(name)
        self._render_material_chips()
        self._refresh_preview()

    @staticmethod
    def _derive_shortcode(name: str) -> str:
        # First letters of each word, uppercased (e.g. "Galvanized Iron" -> "GI").
        parts = [p for p in name.replace("-", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper() if len(parts[0]) >= 2 else parts[0][:1].upper()
        return "".join(p[0] for p in parts[:2]).upper()

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
        self.config.add_client(client)
        self._reload_client_options()
        self.client_dropdown.set(client)
        self._populate_sites(client)
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
            return  # feature disabled
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
        if not result or not any(result.values()):
            # OCR could not read the document — most downloads still carry the
            # Delivery Note number in the file name, so back-fill the serial.
            if self._apply_serial_from_filename():
                self._set_ocr_status(
                    "OCR: could not read document — serial from filename", _SUCCESS
                )
            else:
                self._set_ocr_status("OCR: could not read document")
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
            canonical_client = self._ci_canonical(list(self.config.clients.keys()), client)
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
        status = "Received" if self._received_var.get() else "Submitted"
        try:
            name = fn.build_filename(
                company=self._company_var.get(),
                doc_type=self._doc_type_var.get(),
                site_name=self._current_site(),
                selected_materials=self._selected_materials,
                materials_map=self._materials_map,
                serial=self._serial_var.get(),
                status=status,
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
        # Every saved site lands in the config: near-same spellings resolve to
        # the existing catalog name, and genuinely new sites (typed or from
        # OCR) are added + pushed to GitHub so the next popup offers them.
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
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def show(self) -> None:
        """Wait for the modal popup to be dismissed (blocking).

        Uses wait_window() instead of a nested mainloop(): a nested mainloop()
        on a Toplevel never returns once the window is destroyed, which would
        wedge the controller's popup loop and stop the next queued file from
        ever being shown.
        """
        self.window.wait_window()
