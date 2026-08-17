"""The FilePicker popup dialog.

A top-most modal window that appears when a completed download is detected. It
captures the metadata needed to rename and route the file:

- Company / Site (with an inline "Add New Site" flow)
- Document Type
- Material multi-select (with "Add Material")
- Serial number
- Received Copy checkbox (drives status)
- Save & Organize / Skip buttons
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from .config import ConfigManager

# A sentinel option shown at the bottom of the Site dropdown.
ADD_NEW_SITE_OPTION = "[+ Add New Site...]"
ADD_NEW_MATERIAL_OPTION = "[+ Add Material...]"

# Dark theme colours.
_BG = "#1e1e2e"
_BG_SECONDARY = "#2a2a3c"
_BG_FIELD = "#313244"
_ACCENT = "#7c9bff"
_TEXT = "#e6e6ef"
_TEXT_MUTED = "#a0a0b8"
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
        self.window.title("FilePicker — New Download")
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

        # -- Target file banner -----------------------------------------
        self._banner = ctk.CTkFrame(container, fg_color=_BG_SECONDARY, corner_radius=10)
        self._banner.pack(fill="x", pady=(0, 16))
        self._banner_name = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=15, weight="bold"),
            text_color=_TEXT, wraplength=500, justify="left",
        )
        self._banner_name.pack(anchor="w", padx=14, pady=(12, 2))
        self._banner_size = ctk.CTkLabel(
            self._banner, text="", font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        )
        self._banner_size.pack(anchor="w", padx=14, pady=(0, 12))

        # -- Company ----------------------------------------------------
        ctk.CTkLabel(container, text="Company", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.company_combo = ctk.CTkOptionMenu(
            container, values=[], variable=self._company_var,
            command=self._on_company_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.company_combo.pack(fill="x", pady=(0, 12))

        # -- Site -------------------------------------------------------
        ctk.CTkLabel(container, text="Site", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.site_combo = ctk.CTkOptionMenu(
            container, values=[], variable=self._site_var,
            command=self._on_site_change, fg_color=_BG_FIELD,
            button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.site_combo.pack(fill="x", pady=(0, 12))

        # -- Document type ----------------------------------------------
        ctk.CTkLabel(container, text="Document Type", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.doc_type_combo = ctk.CTkOptionMenu(
            container, values=[], variable=self._doc_type_var,
            command=lambda _d: self._refresh_preview(),
            fg_color=_BG_FIELD, button_color=_ACCENT, button_hover_color=_ACCENT,
        )
        self.doc_type_combo.pack(fill="x", pady=(0, 12))

        # -- Materials (multi-select) -----------------------------------
        ctk.CTkLabel(container, text="Material (multi-select)",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.material_frame = ctk.CTkFrame(container, fg_color=_BG_SECONDARY, corner_radius=8)
        self.material_frame.pack(fill="x", pady=(0, 8))
        self._material_chips: Dict[str, ctk.CTkButton] = {}
        self._render_material_chips()

        # -- Serial number ----------------------------------------------
        ctk.CTkLabel(container, text="Serial Number",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=_TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.serial_entry = ctk.CTkEntry(
            container, textvariable=self._serial_var, fg_color=_BG_FIELD,
            border_color=_BG_FIELD, text_color=_TEXT,
        )
        self.serial_entry.pack(fill="x", pady=(0, 12))

        # -- Received copy checkbox -------------------------------------
        self.received_check = ctk.CTkCheckBox(
            container, text="Received Copy (unchecked = Submitted)",
            variable=self._received_var, fg_color=_ACCENT,
            hover_color=_ACCENT, text_color=_TEXT,
        )
        self.received_check.pack(anchor="w", pady=(0, 16))

        # -- Buttons ----------------------------------------------------
        btn_row = ctk.CTkFrame(container, fg_color=_BG)
        btn_row.pack(fill="x", pady=(8, 0))

        self.save_btn = ctk.CTkButton(
            btn_row, text="Save & Organize", command=self._submit,
            fg_color=_ACCENT, hover_color="#5c7cf0", height=40,
            font=ctk.CTkFont(size=14, weight="bold"), text_color="#ffffff",
        )
        self.save_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.skip_btn = ctk.CTkButton(
            btn_row, text="Skip / Keep Original", command=self._skip,
            fg_color=_BG_FIELD, hover_color="#3d3d52", height=40,
            font=ctk.CTkFont(size=13), text_color=_TEXT_MUTED,
        )
        self.skip_btn.pack(side="left", expand=True, fill="x")

        # -- Live preview ----------------------------------------------
        self.preview_label = ctk.CTkLabel(
            container, text="", font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
            wraplength=520, justify="left",
        )
        self.preview_label.pack(fill="x", pady=(12, 0))
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Config-driven state
    # ------------------------------------------------------------------
    def _reload_config_state(self) -> None:
        data = self.config.load()
        companies = data.get("companies", {})
        self._materials_map = dict(data.get("materials", {}))

        company_names = list(companies.keys())
        if company_names:
            self.company_combo.configure(values=company_names)
            self._company_var.set(company_names[0])
            self._populate_sites(company_names[0])
        else:
            self.company_combo.configure(values=["(no companies)"])
            self._company_var.set("(no companies)")

        doc_types = data.get("doc_types", ["DC"])
        self.doc_type_combo.configure(values=doc_types)
        self._doc_type_var.set(doc_types[0] if doc_types else "DC")

        self._populate_material_options()
        self._render_material_chips()

    def _populate_sites(self, company: str) -> None:
        sites = self.config.sites_for(company)
        values = sites + [ADD_NEW_SITE_OPTION]
        self.site_combo.configure(values=values)
        if sites:
            self._site_var.set(sites[0])
        else:
            self._site_var.set(ADD_NEW_SITE_OPTION)

    def _populate_material_options(self) -> None:
        # Store available material names for the "Add Material" flow.
        self._available_materials = list(self._materials_map.keys())

    # ------------------------------------------------------------------
    # Material chip rendering
    # ------------------------------------------------------------------
    def _render_material_chips(self) -> None:
        for child in self.material_frame.winfo_children():
            child.destroy()
        self._material_chips.clear()

        row = 0
        col = 0
        for name in self._materials_map:
            selected = name in self._selected_materials
            chip = ctk.CTkButton(
                self.material_frame,
                text=f"{name} ({self._materials_map[name]})",
                width=0, height=28,
                fg_color=_ACCENT if selected else _BG_FIELD,
                hover_color=_ACCENT if selected else "#3d3d52",
                text_color="#ffffff" if selected else _TEXT,
                corner_radius=14,
                command=lambda n=name: self._toggle_material(n),
            )
            chip.grid(row=row, column=col, padx=4, pady=4, sticky="w")
            self._material_chips[name] = chip
            col += 1
            if col >= 3:
                col = 0
                row += 1

        # "Add Material" button.
        add_btn = ctk.CTkButton(
            self.material_frame, text="+ Add Material", width=0, height=28,
            fg_color=_BG_FIELD, hover_color="#3d3d52", text_color=_ACCENT,
            corner_radius=14, command=self._prompt_add_material,
        )
        add_btn.grid(row=row + 1, column=0, padx=4, pady=(6, 4), sticky="w")

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
        self._populate_material_options()
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
        self._populate_sites(company)
        self._refresh_preview()

    def _on_site_change(self, site: str) -> None:
        if site == ADD_NEW_SITE_OPTION:
            company = self._company_var.get()
            self._ask_text(
                "Add New Site",
                f"New site name for {company}:",
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
            self._populate_sites(self._company_var.get())
            return
        company = self._company_var.get()
        self.config.add_site(company, site)
        self._populate_sites(company)
        self._site_var.set(site)
        self._refresh_preview()

    # ------------------------------------------------------------------
    # Generic small inline prompt
    # ------------------------------------------------------------------
    def _ask_text(self, title: str, label: str, default: str, placeholder: str,
                  on_ok: Callable[[str], None]) -> None:
        prompt = ctk.CTkToplevel(self.window)
        prompt.title(title)
        prompt.geometry("380x150")
        prompt.configure(fg_color=_BG)
        prompt.transient(self.window)
        prompt.grab_set()
        prompt.attributes("-topmost", True)

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
        from . import filename as fn

        ext = self.file_path.suffix.lstrip(".") or "pdf"
        status = "Received" if self._received_var.get() else "Submitted"
        try:
            name = fn.build_filename(
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
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def show(self) -> None:
        """Enter the Tk event loop for this modal popup (blocking)."""
        self.window.mainloop()
