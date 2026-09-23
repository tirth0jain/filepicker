"""First-run setup dialog for FilePicker.

Shown once when the config file does not exist yet. Asks the user for the
"watch" directory (where downloads land) and the "root" directory (where files
get organised), pre-filled with the defaults from config.json so the user can
just press Save to accept them.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from config import ConfigManager
from version import VERSION

# Palette (matches the rest of the app).
_BG = "#15151d"
_BG_SECONDARY = "#1f1f2b"
_BG_FIELD = "#262633"
_ACCENT = "#5b8cff"
_ACCENT_HOVER = "#3f6fe0"
_TEXT = "#f2f2f7"
_TEXT_MUTED = "#b6b6c9"


def _browse() -> Optional[str]:
    """Open a folder picker; return the chosen path or None if cancelled."""
    return filedialog.askdirectory()


def choose_watch_folder(initial: str = "",
                        heading: str = "Choose your watch folder",
                        note: str = "",
                        button: str = "Save") -> Optional[str]:
    """Ask for ONE folder: where this person's files land.

    Used on a shared install (everybody runs the exe from the same server
    folder), where the watch folder must differ per person — so it cannot live
    in the shared config.json. The answer is remembered in this Windows
    account's own settings file (see localsettings.py) and is what makes the
    popup open on the person who dropped the file and nobody else.

    Returns the chosen path, or None when the dialog was cancelled (the caller
    then keeps the current folder).
    """
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title(f"FilePicker v{VERSION} — Watch Folder")
    root.geometry("560x300")
    root.configure(fg_color=_BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    chosen = {"value": None}
    folder_var = tk.StringVar(value=initial)

    ctk.CTkLabel(
        root, text=heading,
        font=ctk.CTkFont(size=18, weight="bold"), text_color=_TEXT,
    ).pack(anchor="w", padx=24, pady=(24, 4))
    if note:
        ctk.CTkLabel(
            root, text=note, font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
            justify="left", wraplength=500,
        ).pack(anchor="w", padx=24, pady=(0, 14))
    else:
        ctk.CTkLabel(
            root, text="This folder is remembered for your Windows account only.",
            font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
        ).pack(anchor="w", padx=24, pady=(0, 14))

    row = ctk.CTkFrame(root, fg_color="transparent")
    row.pack(fill="x", padx=24, pady=(0, 4))
    entry = ctk.CTkEntry(
        row, textvariable=folder_var, fg_color=_BG_FIELD,
        border_color=_BG_FIELD, text_color=_TEXT,
    )
    entry.pack(side="left", fill="x", expand=True)
    ctk.CTkButton(
        row, text="Browse", width=80, height=30,
        fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT,
        command=lambda: folder_var.set(_browse() or folder_var.get()),
    ).pack(side="left", padx=(8, 0))

    ctk.CTkLabel(
        root,
        text="Tip: the folder your scanner (or browser) writes into — e.g. "
             "Z:\\Your-Name\\…\\tally-dc-source",
        font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
    ).pack(anchor="w", padx=24, pady=(8, 0))

    btn_row = ctk.CTkFrame(root, fg_color="transparent")
    btn_row.pack(fill="x", padx=24, pady=(24, 18))

    def save() -> None:
        value = folder_var.get().strip()
        if value:
            chosen["value"] = value
        root.destroy()

    ctk.CTkButton(
        btn_row, text=button, command=save, height=40,
        fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
        font=ctk.CTkFont(size=14, weight="bold"), text_color="#ffffff",
    ).pack(side="left", expand=True, fill="x", padx=(0, 8))
    ctk.CTkButton(
        btn_row, text="Cancel", command=root.destroy, height=40,
        fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
    ).pack(side="left", expand=True, fill="x")

    entry.focus_set()
    root.bind("<Return>", lambda _e: save())
    root.bind("<Escape>", lambda _e: root.destroy())
    root.grab_set()
    root.lift()
    try:
        root.focus_force()
    except Exception:
        pass
    root.mainloop()
    return chosen["value"]


def run_first_time_setup(config: ConfigManager) -> None:
    """Show a modal first-run dialog for watch/root directories.

    Fields are pre-filled with the current (default) values from config. If the
    user presses Save, the values are written to config.json. Closing the window
    without saving leaves the defaults in place. The app always proceeds after
    this returns.
    """
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title(f"FilePicker v{VERSION} — First Run Setup")
    root.geometry("520x360")
    root.configure(fg_color=_BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    watch_var = tk.StringVar(value=config.watch_directory)
    root_var = tk.StringVar(value=config.root_directory)

    # --- Heading -------------------------------------------------------
    ctk.CTkLabel(
        root, text="Welcome to FilePicker 👋",
        font=ctk.CTkFont(size=18, weight="bold"), text_color=_TEXT,
    ).pack(anchor="w", padx=24, pady=(24, 4))
    ctk.CTkLabel(
        root, text="Let's set up your folders. The defaults are shown below.",
        font=ctk.CTkFont(size=12), text_color=_TEXT_MUTED,
    ).pack(anchor="w", padx=24, pady=(0, 16))

    # --- Watch directory ----------------------------------------------
    ctk.CTkLabel(
        root, text="Watch folder (where downloads land)",
        font=ctk.CTkFont(size=13, weight="bold"), text_color=_TEXT_MUTED,
    ).pack(anchor="w", padx=24, pady=(0, 4))

    watch_row = ctk.CTkFrame(root, fg_color="transparent")
    watch_row.pack(fill="x", padx=24, pady=(0, 4))
    watch_entry = ctk.CTkEntry(
        watch_row, textvariable=watch_var, fg_color=_BG_FIELD,
        border_color=_BG_FIELD, text_color=_TEXT,
    )
    watch_entry.pack(side="left", fill="x", expand=True)
    ctk.CTkButton(
        watch_row, text="Browse", width=80, height=30,
        fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT,
        command=lambda: watch_var.set(_browse() or watch_var.get()),
    ).pack(side="left", padx=(8, 0))

    # --- Root directory ------------------------------------------------
    ctk.CTkLabel(
        root, text="Root folder (where files get organised)",
        font=ctk.CTkFont(size=13, weight="bold"), text_color=_TEXT_MUTED,
    ).pack(anchor="w", padx=24, pady=(14, 4))

    root_row = ctk.CTkFrame(root, fg_color="transparent")
    root_row.pack(fill="x", padx=24, pady=(0, 4))
    root_entry = ctk.CTkEntry(
        root_row, textvariable=root_var, fg_color=_BG_FIELD,
        border_color=_BG_FIELD, text_color=_TEXT,
    )
    root_entry.pack(side="left", fill="x", expand=True)
    ctk.CTkButton(
        root_row, text="Browse", width=80, height=30,
        fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT,
        command=lambda: root_var.set(_browse() or root_var.get()),
    ).pack(side="left", padx=(8, 0))

    # --- Hint ----------------------------------------------------------
    ctk.CTkLabel(
        root,
        text="Tip: the watch folder is usually your browser's Downloads folder.",
        font=ctk.CTkFont(size=11), text_color=_TEXT_MUTED,
    ).pack(anchor="w", padx=24, pady=(8, 0))

    # --- Buttons -------------------------------------------------------
    btn_row = ctk.CTkFrame(root, fg_color="transparent")
    btn_row.pack(fill="x", padx=24, pady=(24, 18))

    def save() -> None:
        config.set_watch_directory(watch_var.get().strip() or config.watch_directory)
        config.set_root_directory(root_var.get().strip() or config.root_directory)
        root.destroy()

    ctk.CTkButton(
        btn_row, text="Save & Start", command=save, height=40,
        fg_color=_ACCENT, hover_color=_ACCENT_HOVER,
        font=ctk.CTkFont(size=14, weight="bold"), text_color="#ffffff",
    ).pack(side="left", expand=True, fill="x", padx=(0, 8))
    ctk.CTkButton(
        btn_row, text="Use Defaults", command=root.destroy, height=40,
        fg_color=_BG_FIELD, hover_color="#33334a", text_color=_TEXT_MUTED,
    ).pack(side="left", expand=True, fill="x")

    root.grab_set()
    root.mainloop()