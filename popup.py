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
ADD_NEW_COMPANY_OPTION = "[+ Add New Company...]"
ADD_NEW_MATERIAL_OPTION = "[+ Add Material...]"

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
        self._site_var = tk.StringVar()
        self._doc_type_var = tk.StringVar(value="DC")
        self._serial_var = tk.StringVar()
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
            self.window.geometry("560x720")
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
            self.window.geometry("560x720")
            print(f"[filepicker] preview error: {exc}")
            return
        self.window.geometry("1180x720")
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
        self.window.geometry("560x720")
        self.window.configure(fg_color=_BG)
        self.window.resizable(False, False)
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
        self.client_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._client_var,
            command=self._on_client_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.client_combo.pack(fill="x", pady=(0, 12))

        # -- Site -------------------------------------------------------
        ctk.CTkLabel(f, text="Site", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.site_combo = ctk.CTkOptionMenu(
            f, values=[], variable=self._site_var,
            command=self._on_site_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.site_combo.pack(fill="x", pady=(0, 12))

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

        # Client dropdown drives the site list.
        client_names = list(clients.keys())
        if client_names:
            self.client_combo.configure(values=client_names)
            self._client_var.set(client_names[0])
            self._populate_sites(client_names[0])
        else:
            self.client_combo.configure(values=["(no clients)"])
            self._client_var.set("(no clients)")

        doc_types = data.get("doc_types", ["DC"])
        self.doc_type_combo.configure(values=doc_types)
        self._doc_type_var.set(doc_types[0] if doc_types else "DC")

        self._render_material_chips()
        self._refresh_preview()

    def _populate_sites(self, client: str) -> None:
        sites = self.config.sites_for(client)
        values = sites + [ADD_NEW_SITE_OPTION]
        self.site_combo.configure(values=values)
        if sites:
            self._site_var.set(sites[0])
        else:
            self._site_var.set(ADD_NEW_SITE_OPTION)

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
        self._populate_sites(client)
        self._refresh_preview()

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
        self._site_var.set(site)
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
                site_name=self._site_var.get(),
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
            "client": self._client_var.get(),
            "site": self._site_var.get(),
            "doc_type": self._doc_type_var.get(),
            "materials": list(self._selected_materials),
            "serial": self._serial_var.get(),
            "status": status,
        }
        self._release()
        self.on_submit(payload)

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
