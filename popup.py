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

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
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
        self._after_id = None

        self.entry = ctk.CTkEntry(
            self, fg_color=_BG_FIELD, border_color=_BG_FIELD,
            text_color=_TEXT, placeholder_text="Search or select…",
        )
        if width:
            self.entry.configure(width=width)
        self.entry.pack(fill="x", pady=(0, 12))

        # Dropdown window (overrideredirect Toplevel with a Listbox)
        self._top = tk.Toplevel(self)
        self._top.withdraw()
        self._top.overrideredirect(True)
        self._top.attributes("-topmost", True)
        # Do not take focus away from the entry when showing.
        try:
            self._top.attributes("-type", "tooltip")
        except tk.TclError:
            pass
        self._top.configure(bg=_BG_FIELD)
        self._listbox = tk.Listbox(
            self._top, bg=_BG_FIELD, fg=_TEXT,
            selectbackground=_ACCENT, selectforeground="#ffffff",
            activestyle="none", highlightthickness=0, bd=0,
            font=tkfont.Font(family="Segoe UI", size=11),
            exportselection=False,
        )
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)
        self._listbox.bind("<ButtonRelease-1>", self._on_list_click)
        self._listbox.bind("<Motion>", lambda e: None)

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

    # ------------------------------------------------------------------
    def get(self) -> str:
        """The currently displayed value: the selected match if set, otherwise
        whatever the user typed (so typing + Save without clicking still uses
        the typed value). Resolves typed text to the canonical case when it
        matches an existing value case-insensitively."""
        typed = self.entry.get().strip()
        if typed:
            # If typed text matches a value case-insensitively, return the
            # canonical value (e.g. "alpha infra" -> "Alpha Infra").
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

    # ------------------------------------------------------------------
    def _visible_items(self) -> list:
        text = self.entry.get().strip().lower()
        if not text:
            matches = list(self._values)
        else:
            matches = [v for v in self._values if text in v.lower()]
        return matches[:_MAX_DROPDOWN_RESULTS]

    def _full_matches(self) -> list:
        text = self.entry.get().strip().lower()
        if not text:
            return list(self._values)
        return [v for v in self._values if text in v.lower()]

    def _show_all(self) -> None:
        self._update_listbox()
        self._open()

    def _open(self) -> None:
        if self._dropdown_open:
            self._update_listbox()
            self._reposition()
            return
        self._update_listbox()
        if self._listbox.size() == 0:
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
        # Delay so a click on the listbox registers before we hide.
        self.after(180, self._close_if_not_focused)

    def _close_if_not_focused(self) -> None:
        try:
            focused = self.focus_displayof()
            if focused is not None and str(focused).startswith(str(self._top)):
                return
            # Also check if the listbox itself has focus
            if self._listbox is not None:
                try:
                    if self._listbox.focus_get() is not None and str(self._listbox.focus_get()).startswith(str(self._top)):
                        return
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        self._close()

    def _update_listbox(self) -> None:
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
        # Highlight first real item
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
        # Skip disabled "(no matches)" / "… more" rows
        self._highlight_index = (self._highlight_index + delta) % n
        # Skip disabled last row
        val = self._listbox.get(self._highlight_index)
        if val.startswith("… ") or val == "(no matches)":
            self._highlight_index = (self._highlight_index + delta) % n
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(self._highlight_index)
        self._listbox.activate(self._highlight_index)
        self._listbox.see(self._highlight_index)
        return "break"

    def _on_return(self, _e=None) -> str:
        if self._dropdown_open and self._listbox.size() > 0:
            idx = self._highlight_index if 0 <= self._highlight_index < self._listbox.size() else 0
            val = self._listbox.get(idx)
            if not val.startswith("… ") and val != "(no matches)":
                self._choose(val)
                return "break"
        return ""

    def _on_key(self, _e=None) -> None:
        # Live search: re-filter on every keystroke (no Enter needed).
        # Keep entry focused; listbox never steals it.
        self._update_listbox()
        if self._listbox.size() > 0 and self._listbox.get(0) != "(no matches)":
            self._reposition()
            if not self._dropdown_open:
                try:
                    self._top.deiconify()
                    self._top.lift()
                    self._dropdown_open = True
                except tk.TclError:
                    pass
            self.entry.focus_set()
        else:
            self._close()
        # Do NOT return "break" — let the key go to the entry

    def _on_focus(self, _e=None) -> None:
        """Show the dropdown as soon as the field is focused, so typing starts
        from a visible, live list (no need to press Enter first)."""
        self._update_listbox()
        if self._listbox.size() > 0:
            self._open()

    def _key_pressed(self, _e=None) -> None:
        """Bound to a <Key> event: same live refresh as typing."""
        self._on_key()


class FilePickerPopup:
    """Modal dialog that gathers metadata and hands it to a callback."""

    def __init__(
        self,
        config: ConfigManager,
        file_path: Path,
        on_submit: Callable[[dict], None],
        on_skip: Callable[[], None],
    ) -> None:
        self.config = config
        self.file_path = Path(file_path)
        self.on_submit = on_submit
        self.on_skip = on_skip

        # Internal UI state.
        self._company_var = tk.StringVar()
        self._client_var = tk.StringVar()
        self._doc_type_var = tk.StringVar(value="DC")
        self._serial_var = tk.StringVar()
        self._tags_var = tk.StringVar()
        self._received_var = tk.BooleanVar(value=True)
        self._selected_materials: List[str] = []

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
            self.preview_pane.pack_forget()
            self.window.geometry("560x820")
            self.preview_btn.configure(text="👁 Preview")
            return

        # --- Open the preview: embed it and expand the window right. ---
        self.preview_pane.pack(side="left", fill="both", expand=True)
        try:
            self._preview = PreviewWindow(
                self.window, self.file_path, container=self.preview_pane
            )
        except Exception as exc:
            self._preview = None
            self.preview_pane.pack_forget()
            self.window.geometry("560x820")
            print(f"[filepicker] preview error: {exc}")
            return
        self.window.geometry("1180x820")
        self.preview_btn.configure(text="✕ Close Preview")

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
        self.window.geometry("560x820")
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
        container.pack(fill="both", expand=True, padx=18, pady=18)

        # Horizontal body: the metadata form on the left, and a preview pane
        # on the right that the window expands into when Preview is opened.
        body = ctk.CTkFrame(container, fg_color=_BG)
        body.pack(fill="both", expand=True)

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
        self._banner.pack(fill="x", pady=(0, 16))

        banner_header = ctk.CTkFrame(self._banner, fg_color="transparent")
        banner_header.pack(fill="x", padx=14, pady=(12, 2))
        self._banner_name = ctk.CTkLabel(
            banner_header, text="", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT, wraplength=380, justify="left",
        )
        self._banner_name.pack(side="left", anchor="w")
        self.preview_btn = ctk.CTkButton(
            banner_header, text="👁 Preview", width=96, height=30,
            fg_color=_ACCENT, hover_color=_ACCENT_HOVER, text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"), command=self._toggle_preview,
        )
        self.preview_btn.pack(side="right", anchor="e")

        self._banner_size = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        )
        self._banner_size.pack(anchor="w", padx=14, pady=(0, 12))

        # -- Company ----------------------------------------------------
        ctk.CTkLabel(f, text="Company", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.company_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._company_var,
            command=self._on_company_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.company_combo.pack(fill="x", pady=(0, 12))

        # -- Client -----------------------------------------------------
        ctk.CTkLabel(f, text="Client", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.client_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_client_change,
        )
        self.client_dropdown.entry.configure(
            placeholder_text="Search client…",
        )

        # -- Site -------------------------------------------------------
        ctk.CTkLabel(f, text="Site", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.site_dropdown = SearchableDropdown(
            f, values=[], on_change=self._on_site_change,
        )
        self.site_dropdown.entry.configure(
            placeholder_text="Search site…",
        )

        # -- Document type ----------------------------------------------
        ctk.CTkLabel(f, text="Document Type", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.doc_type_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._doc_type_var,
            command=lambda _d: self._refresh_preview(),
            fg_color=_BG_FIELD, button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.doc_type_combo.pack(fill="x", pady=(0, 12))

        # -- Materials (multi-select) -----------------------------------
        ctk.CTkLabel(f, text="Material (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.material_frame = ctk.CTkFrame(f, fg_color=_BG_SECONDARY, corner_radius=8)
        self.material_frame.pack(fill="x", pady=(0, 8))
        self._material_chips: Dict[str, ctk.CTkButton] = {}
        self._render_material_chips()

        # -- Serial number ----------------------------------------------
        ctk.CTkLabel(f, text="Serial Number",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.serial_entry = ctk.CTkEntry(
            f, textvariable=self._serial_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT,
        )
        self.serial_entry.pack(fill="x", pady=(0, 12))

        # -- Tags (searchable metadata) ----------------------------------
        ctk.CTkLabel(f, text="Tags (search in Windows Explorer)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        ctk.CTkEntry(
            f, textvariable=self._tags_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT,
            placeholder_text="e.g. Aluminium, DC, Site 1  (comma separated)",
        ).pack(fill="x", pady=(0, 12))

        # -- Received copy checkbox -------------------------------------
        self.received_check = ctk.CTkCheckBox(
            f, text="Received Copy (unchecked = Submitted)",
            variable=self._received_var, fg_color=_ACCENT,
            hover_color=_ACCENT, text_color=_TEXT,
        )
        self.received_check.pack(anchor="w", pady=(0, 16))

        # -- Buttons ----------------------------------------------------
        btn_row = ctk.CTkFrame(f, fg_color=_BG)
        btn_row.pack(fill="x", pady=(8, 0))

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
        self.preview_label.pack(fill="x", pady=(12, 0))
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

        # Client dropdown drives the site list. Values include the "add new"
        # sentinel so a new client can be created right from the dropdown.
        client_names = list(clients.keys())
        if client_names:
            self.client_dropdown.configure(values=client_names + [ADD_NEW_CLIENT_OPTION])
            self.client_dropdown.set(client_names[0])
            self._client_var.set(client_names[0])
            self._populate_sites(client_names[0])
        else:
            self.client_dropdown.configure(values=[ADD_NEW_CLIENT_OPTION])
            self.client_dropdown.set(ADD_NEW_CLIENT_OPTION)
            self._client_var.set(ADD_NEW_CLIENT_OPTION)

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
    # Material chip rendering
    # ------------------------------------------------------------------
    def _render_material_chips(self) -> None:
        for child in self.material_frame.winfo_children():
            child.destroy()
        self._material_chips.clear()

        # Measure text so chips pack tightly with no big gaps between them.
        measure_font = tkfont.Font(family="Segoe UI", size=13)
        wrap_width = 500
        row_frame = ctk.CTkFrame(self.material_frame, fg_color="transparent")
        row_frame.pack(fill="x", padx=8, pady=8)
        row_width = 0

        def place_chip(text, fg, hover, txt, command):
            nonlocal row_frame, row_width
            chip = ctk.CTkButton(
                row_frame, text=text, width=0, height=28,
                fg_color=fg, hover_color=hover, text_color=txt,
                corner_radius=14, command=command,
            )
            est = measure_font.measure(text) + 28  # text + padding
            if row_width + est > wrap_width:
                row_frame = ctk.CTkFrame(self.material_frame, fg_color="transparent")
                row_frame.pack(fill="x", padx=8, pady=(0, 8))
                row_width = 0
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

    def _toggle_material(self, name: str) -> None:
        if name in self._selected_materials:
            self._selected_materials.remove(name)
            self._remove_tag(name)
        else:
            self._selected_materials.append(name)
            self._add_tag(name)
        self._render_material_chips()
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Auto tags (case-insensitive)
    # ------------------------------------------------------------------
    def _current_tags(self) -> List[str]:
        """Parse the tags field into a list (deduped, case-insensitive)."""
        raw = self._tags_var.get()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        seen, out = set(), []
        for p in parts:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _set_tags(self, tags: List[str]) -> None:
        self._tags_var.set(", ".join(tags))

    def _add_tag(self, tag: str) -> None:
        tags = self._current_tags()
        if tag and not any(t.lower() == tag.lower() for t in tags):
            tags.append(tag)
            self._set_tags(tags)

    def _remove_tag(self, tag: str) -> None:
        tags = self._current_tags()
        kept = [t for t in tags if t.lower() != tag.lower()]
        if len(kept) != len(tags):
            self._set_tags(kept)

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
        self.config.add_site(client, site)
        self._populate_sites(client)
        self.site_dropdown.set(site)
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
        payload = {
            "file_path": self.file_path,
            "company": self._company_var.get(),
            "client": self.client_dropdown.get(),
            "site": self._current_site(),
            "doc_type": self._doc_type_var.get(),
            "materials": list(self._selected_materials),
            "serial": self._serial_var.get(),
            "status": status,
            "tags": self._tags_var.get(),
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
        # Close the preview first so it releases the file handle; otherwise the
        # source file stays locked on Windows and can't be deleted afterwards.
        if self._preview is not None:
            try:
                self._preview.destroy()
            except Exception:
                pass
            self._preview = None
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
