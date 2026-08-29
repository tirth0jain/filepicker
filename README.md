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
- **File preview** — a **👁 Preview** button in the popup expands the window to
  the right and shows the file preview **embedded in the same window** (no
  separate window). Click **✕ Close Preview** to collapse it back:
  - **PDFs** (PyMuPDF) — page-by-page with Prev/Next, plus zoom controls
    (**+ / − / Fit Width** or **Ctrl + mouse wheel**). Pages are rendered at a
    low base DPI for fast text loading and PNG-compressed (a 2 MB raw page
    becomes ~14 KB), so even 50+ page documents stay light in memory. Zooming
    re-renders at a higher DPI so text stays crisp and readable.
  - **Images** (Pillow) — zoomable, PNG-compressed, scrollable.
  - **Excel** (openpyxl/xlrd) — shown as a table with a sheet selector.
- **Strict filename format** —
  `{Company}-{Doc Type}-{FY}-{Site Name}-{Material Shortcodes}-{Serial}-{Status}.{ext}`
  e.g. `Acme-DC-26-27-Site 1 - Mumbai-A+C-0001-Received.pdf`.
- **Financial Year** auto-calculated for the Indian fiscal year (Apr 1–Mar 31):
  Aug 2026 → `26-27`, Feb 2026 → `25-26`.
- **Directory routing** — copies the file (once) into:
  `[root]/[Company]/[Client]/[Site]/[Doc Type]/[Received or Submitted]/[filename]`,
  plus an extra copy into `[root]/[Company]/All DC/[Received or Submitted]/` whenever the
  Doc Type is `DC`. Collisions are handled with a `_1`, `_2`, … suffix (never
  blindly overwritten).
- **Config persistence** — all companies, clients, sites, materials and doc
  types are read from and written back to `config.json` dynamically.
- **OCR auto-fill (optional)** — set `"enable_ocr": true` in `config.json` and
  the popup automatically reads each delivery note (PDF or image) with the
  **DeepSeek V4 Flash Vision Exp** vision model and pre-fills **Company
  (Supplier) / Client (Buyer) / Site (Other References)** so you only have to
  verify and hit *Save & Organize*. Uses your **OpenCode Go** subscription API
  key — see [OCR setup](#ocr-setup-deepseek-v4-flash-vision-exp).

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
  "companies": ["Acme Corp", "Beta Industries"],
  "clients": {
    "Alpha Infra": ["Site 1 - Mumbai", "Site 2 - Pune"],
    "Beta Projects": ["Plant Central"]
  }
}
```

New companies, clients, sites, materials and doc types added from the UI are
saved back to this file automatically. The first entry in `companies` is the
default shown in the popup's Company dropdown.

## OCR setup (DeepSeek V4 Flash Vision Exp)

When enabled, every new download opens the popup already pre-filled with the
supplier / buyer / site read from the document — no manual typing.

1. **Get the key** — the same API key your opencode CLI uses
   (`opencode auth`). It is the OpenCode Go subscription key, *not* a GitHub
   token.
2. **Give it to FilePicker** — one of:
   - create `opencode_token.txt` next to `FilePicker.exe` (first line: the
     key, optionally `token = <key>`), or
   - set the environment variable `FILEPICKER_OPENCODE_TOKEN` (or
     `OPENCODE_API_KEY`). In dev, the key from
     `~/.local/share/opencode/auth.json` is used automatically.
3. **Turn the feature on** in `config.json`:

   ```json
   {
     "enable_ocr": true,
     "ocr_model": "deepseek-v4-flash-vision-exp",
     "ocr_api_base": "https://opencode.ai/zen/go/v1"
   }
   ```

   `enable_ocr` is a **local-only** flag: it is never synced from the GitHub
   config and never pushed back, because OCR needs this machine's own key.
   The model/endpoint defaults above can be overridden per machine.

While OCR runs, the popup shows `OCR: reading document…`. Results are
applied only if you haven't started typing; names that already exist in the
catalog are matched case-insensitively (canonical spelling is used), and
brand-new names stay typed so you can review (and optionally *Add*) them
before saving. The Serial Number is read from the **Delivery Note No.** field
(e.g. `RS/DC/26-27/6` → `6`) and, when OCR can't read it, is back-filled from
the file name (`RS-DC-26-27-6.pdf` → `6`). The key never lands in
`config.json`, so it can't leak to the public repo.

**Batches of 10, paced to your review:** files are OCR'd in batches of
**10 concurrent vision calls**, and the next batch starts only once you are
checking the last file of the current one — after 9 saves the next 10 are
already being read in the background, so each popup is pre-filled by the
time you get to it. A folder of 20 notes never fires more than 10 vision
calls at once, and OCR doesn't run ahead of what you're actually reviewing.

## Usage

**From source (dev):**

```bash
python main.py
```

**Compiled .exe:** double-click `FilePicker.exe`. It was built with
`--windows-console-mode=disable`, so **no terminal window appears** — the app
runs silently in the background (hidden main window) and pops up the metadata
dialog whenever a download completes. A **system tray icon** (📄) provides a
**Check for updates** action (manual update trigger) and a **Quit** option.

**First-run setup:** on the very first launch the app shows a one-time dialog
asking for your **watch folder** (where downloads land) and **root folder**
(where files get organised), pre-filled with the defaults from `config.json`
so you can just press **Save & Start** to accept them (or Browse to change).

**Auto-start at Windows login:** the app **auto-registers itself on first run**
— it creates a Startup-folder shortcut automatically (via PowerShell, no extra
dependencies), so it launches at every login with no manual step. To control it:

- Disable auto-start: set `"auto_start": false` in `config.json`.
- Manual control: `FilePicker.exe --install-startup` / `FilePicker.exe --remove-startup`.
- Manual alternative: press `Win+R`, type `shell:startup`, and drop a shortcut
  to `FilePicker.exe` in the folder that opens.

## Uninstalling

FilePicker is **portable** — there is no installer and it writes **nothing to
the Windows registry**. To remove it completely:

1. **Remove it from startup** (so it stops launching at login):
   `FilePicker.exe --remove-startup`
   (or press `Win+R`, type `shell:startup`, and delete the `FilePicker.lnk` shortcut).
2. **Stop it if it's running** — close it from Task Manager.
3. **Delete the app folder** — the `.exe` and everything next to it
   (`config.json`, `installed_version.txt`, `last_update.txt` if present).
4. **Optionally delete the organised data** — the `root_directory` you set in
   `config.json` (e.g. `D:/Company_Data`) contains all the copied/organised
   files. Delete it only if you don't want to keep them.

That's it — no registry keys, no services, no leftover system entries.

## Updating the version number

The version lives in **one place**: `version.py` (`VERSION = "0.1.2"`). Every
window title, the update popup, the Nuitka binary metadata, the release tag and
the updater all read it from there. To bump the version:

1. Edit `version.py` → change `VERSION = "0.1.2"` to the new value (e.g. `"0.1.3"`).
2. Commit + push. CI builds a release tagged `v0.1.3-<sha>` automatically, and
   installed copies auto-update to it.

No other file needs changing — the rest all import `VERSION`.

## Filename rules

- **Financial Year (FY)** — Indian fiscal year (Apr 1 – Mar 31).
  - Month ≥ April: `YY-(YY+1)` (e.g. Aug 2026 → `26-27`).
  - Month < April: `(YY-1)-YY` (e.g. Feb 2026 → `25-26`).
- **Material shortcodes** — multiple materials joined with `+`, e.g. `A+C`.
- **Status** — `Received` when *Received Copy* is checked, else `Submitted`.
- **Sanitisation** — illegal Windows characters `\ / : * ? " < > |` are removed,
  and trailing dots/spaces are stripped.

## Building a standalone app (no Python needed on target)

Compile with **Nuitka** into a **standalone folder** (not a packed onefile) and
zip it:

```bash
pip install -r requirements.txt
pip install nuitka
python build.py        # or run build.bat
```

This produces `dist/FilePicker-<version>-win64.zip` — a folder containing
`FilePicker.exe` + bundled libraries, with Python embedded, so target machines
need no Python installation. To install: extract the zip and run
`FilePicker.exe`.

### Why standalone (a folder) instead of onefile?

Nuitka's `--onefile` build is a packed, self-extracting bootstrap that unpacks
to a temp folder at runtime and runs from there. That behavior matches a common
malware-dropper signature, so heuristic antivirus engines (Windows Defender,
VirusTotal) frequently flag it as a **false positive**. A **standalone folder**
has no self-extractor, which removes that main heuristic trigger and greatly
reduces false positives. The auto-updater downloads the new zip, extracts it,
swaps the app folder, and relaunches — the same experience, minus the AV noise.

### Windows SmartScreen / Defender

- On the **first** run of a browser-downloaded exe, SmartScreen may warn
  ("unknown publisher") because it's unsigned. Click **More info → Run anyway**.
- **Auto-updates** are downloaded by the app itself (not a browser), so they
  don't carry the download "Mark of the Web" tag and generally don't re-trigger
  the warning.
- The standalone build is the free mitigation for the Defender false positive.
  For the definitive fix, code-sign the binary (OV/EV certificate).

## Auto-update via GitHub Releases

- The app checks **GitHub Releases** for a newer binary at startup and every
  5 minutes (`updater.py`). The repository is `tirth0jain/filepicker`.
- Each release is tagged `v<version>-<commit-sha>`; the app stores the tag it
  is running in `installed_version.txt` next to the binary, so every new commit
  triggers an update.
- **Safe update timing:** the new binary is downloaded as soon as an update is
  detected, but the swap is **deferred until the app is fully idle** — it waits
  for any open popup, queued file, or in-progress organise operation to finish
  before replacing the running exe and relaunching. No work is ever interrupted.
- On update: the new `.exe` is swapped in (running binary renamed to `.old`),
  and the app relaunches.
- **Leftover cleanup:** files locked by the still-running process are renamed
  to `.old`, and the freshly launched process removes all `.old` files at
  first startup (`resume_pending_update`) — leftovers never accumulate, even
  if a swap is interrupted.
- **After an update, a popup appears** telling you what version it was updated
  from and to (e.g. `v0.1.2-aaa -> v0.1.2-bbb`).
- The current version is shown in the title bar of every window
  (e.g. `FilePicker v0.1.2 — New Download`).

## CI: auto-compile on every commit

`.github/workflows/build.yml` builds the app with Nuitka on every push to
`main` and uploads the `.exe` to GitHub Releases, so a fresh binary is always
available and the updater picks it up automatically.

## Project layout

```
filepicker/
├── main.py          # entry point & controller
├── config.py        # ConfigManager (load/save config.json)
├── watcher.py       # watchdog-based folder watcher + lock debounce
├── popup.py         # customtkinter metadata popup
├── ocr.py           # OCR auto-fill (OpenCode Go DeepSeek V4 Flash Vision Exp)
├── viewer.py        # lightweight PDF / image / Excel preview window
├── filename.py      # filename formatting & collision resolution
├── organizer.py     # directory routing & file distribution
├── updater.py       # GitHub Releases auto-update (check + atomic swap)
├── tray.py          # system tray icon + "Check for updates" / Quit menu
├── startup.py       # Windows auto-start (Startup-folder shortcut)
├── setup.py         # one-time first-run setup dialog (watch/root folders)
├── version.py       # app version (0.1.2)
├── build.py         # Nuitka build script
├── build.bat        # Windows build shortcut
├── config.json      # persistent configuration
├── requirements.txt
└── .github/workflows/build.yml   # CI: build + release on every commit
```