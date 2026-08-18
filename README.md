# FilePicker

A lightweight, non-intrusive Windows background utility that monitors a download
folder, detects completed files, displays a metadata popup, renames the file
according to a strict standard, and routes copies into structured directories.

Built with **Python 3.10+**, **customtkinter** (modern dark UI) and **watchdog**
(folder monitoring).

---

## Features

- **Folder watcher** — monitors `watch_directory` and ignores temporary files
  (`.crdownload`, `.part`, `.tmp`, `.download`, hidden files). Waits for the
  file size to stabilise and write locks to be released before triggering the
  popup. Simultaneous downloads are queued and handled one at a time.
- **Metadata popup** — a top-most modal dialog showing the target file banner,
  Company / Site dropdowns (with an inline *Add New Site* flow), Document Type,
  Material multi-select (with *Add Material*), Serial Number, a *Received Copy*
  checkbox, and a live filename preview.
- **File preview** — a **👁 Preview** button in the popup opens a lightweight
  non-modal viewer next to the popup to verify the download before organising:
  - **PDFs** (PyMuPDF) — page-by-page with Prev/Next, plus zoom controls
    (**+ / − / Fit Width** or **Ctrl + mouse wheel**). Pages are rendered at a
    low base DPI for fast text loading and PNG-compressed (a 2 MB raw page
    becomes ~14 KB), so even 50+ page documents stay light in memory. Zooming
    re-renders at a higher DPI so text stays crisp and readable.
  - **Images** (Pillow) — zoomable, PNG-compressed, scrollable.
  - **Excel** (openpyxl/xlrd) — shown as a table with a sheet selector.
- **Strict filename format** —
  `{Doc Type}-{FY}-{Site Name}-{Material Shortcodes}-{Serial}-{Status}.{ext}`
  e.g. `DC-26-27-Site 1 - Mumbai-A+C-0001-Received.pdf`.
- **Financial Year** auto-calculated for the Indian fiscal year (Apr 1–Mar 31):
  Aug 2026 → `26-27`, Feb 2026 → `25-26`.
- **Directory routing** — copies the file into each selected material folder:
  `[root]/[Company]/[Site]/[Doc Type]/[Material Name]/[Received or Submitted]/[filename]`,
  plus an extra copy into `[root]/All DC/[Received or Submitted]/` whenever the
  Doc Type is `DC`. Collisions are handled with a `_1`, `_2`, … suffix (never
  blindly overwritten).
- **Config persistence** — all companies, sites, materials and doc types are
  read from and written back to `config.json` dynamically.

---

## Installation

Requires **Python 3.10+** and a Windows machine.

```bash
cd filepicker
pip install -r requirements.txt
```

## Configuration

Edit `config.json` (next to the app) to set your folders and options:

```json
{
  "watch_directory": "C:/Users/<Username>/Downloads",
  "root_directory": "D:/Company_Data",
  "doc_types": ["DC", "Tax Invoice", "Purchase Order", "MTC"],
  "materials": {
    "Aluminium": "A",
    "Carbon": "C",
    "Stainless Steel": "SS",
    "Mild Steel": "MS",
    "Galvanized Iron": "GI"
  },
  "companies": {
    "Alpha Infra": ["Site 1 - Mumbai", "Site 2 - Pune"],
    "Beta Projects": ["Plant Central"]
  }
}
```

New companies, sites, materials and doc types added from the UI are saved back
to this file automatically.

## Usage

```bash
python main.py
```

The app stays resident in the background (hidden main window). When a download
completes, the popup appears. Fill in the details and press **Save & Organize**,
or **Skip / Keep Original** to leave the file untouched in the watch folder.

## Building a standalone .exe (no Python needed on target)

Compile with **Nuitka** into a single portable Windows executable:

```bash
pip install -r requirements.txt
pip install nuitka
python build.py        # or run build.bat
```

The binary lands in `dist/` with Python embedded, so target machines need no
Python installation.

## Auto-update via GitHub Releases

- The app checks **GitHub Releases** for a newer binary at startup and every
  6 hours (`updater.py`).
- Set your repository in `updater.py`: `GITHUB_REPO = "yourname/filepicker"`.
- Each release is tagged `v<version>-<commit-sha>`; the app stores the tag it
  is running in `installed_version.txt` next to the binary, so every new commit
  triggers an update.
- On update: the new `.exe` is downloaded, the running binary is renamed to
  `.old`, the new one is swapped in, and the app relaunches. If anything fails
  the original binary is restored.

## CI: auto-compile on every commit

`.github/workflows/build.yml` builds the app with Nuitka on every push to
`main` and uploads the `.exe` to GitHub Releases, so a fresh binary is always
available and the updater picks it up automatically.

## Filename rules

- **Financial Year (FY)** — Indian fiscal year (Apr 1 – Mar 31).
  - Month ≥ April: `YY-(YY+1)` (e.g. Aug 2026 → `26-27`).
  - Month < April: `(YY-1)-YY` (e.g. Feb 2026 → `25-26`).
- **Material shortcodes** — multiple materials joined with `+`, e.g. `A+C`.
- **Status** — `Received` when *Received Copy* is checked, else `Submitted`.
- **Sanitisation** — illegal Windows characters `\ / : * ? " < > |` are removed,
  and trailing dots/spaces are stripped.

## Project layout

```
filepicker/
├── main.py          # entry point & controller
├── config.py        # ConfigManager (load/save config.json)
├── watcher.py       # watchdog-based folder watcher + lock debounce
├── popup.py         # customtkinter metadata popup
├── viewer.py        # lightweight PDF / image / Excel preview window
├── filename.py      # filename formatting & collision resolution
├── organizer.py     # directory routing & file distribution
├── updater.py       # GitHub Releases auto-update (check + atomic swap)
├── version.py       # app version (0.1.0)
├── build.py         # Nuitka build script
├── build.bat        # Windows build shortcut
├── config.json      # persistent configuration
├── requirements.txt
└── .github/workflows/build.yml   # CI: build + release on every commit
```
# filepicker
# filepicker
