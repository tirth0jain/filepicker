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
  `{Company}-{Doc Type}-{FY}-{Serial}-{Site Name}-{Material Shortcodes}.{ext}`
  e.g. `Acme-DC-26-27-0001-Site 1 - Mumbai-A+C.pdf`.
  The Received/Submitted status is deliberately *not* in the filename — it is
  reflected only in the destination folder
  (`.../[Doc Type]/[Received or Submitted]/`).
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
  every download is read the moment it lands (PDF or image, all files at once)
  with the **DeepSeek V4.1 Flash** model, pre-filling **Company (Supplier) /
  Client (Buyer) / Site (Other References)** so you only have to verify and hit
  *Save & Organize*. Uses your **OpenCode Go** subscription API key — see
  [OCR setup](#ocr-setup-deepseek-v41-flash).
- **Keyboard-first** — the popup takes the keyboard the moment it opens (no
  click needed), so its shortcuts work immediately: **Ctrl+S** Save & Organize,
  **Ctrl+Delete** Skip, **Ctrl+P** Preview, and **Alt + a material's 2-letter
  code** (Alt+AL = Aluminium) to toggle a material from any field. Shortcuts
  are **Caps Lock-proof**: Tk matches a key by its keysym, and Caps Lock turns
  Ctrl+S into keysym `S`, so every letter shortcut is bound in both spellings
  (and the Alt chord is case-folded) — Caps Lock on or off, the same keys
  work. Question
  dialogs (duplicate file, site of another client) show a letter on every
  option — press **Ctrl+Y / Ctrl+N / Ctrl+M** to answer instantly, or move the
  selection with the **arrow keys** and press **Enter**.
  The popup also verifies a moment after opening that it is really on screen
  and re-shows itself if Windows left it hidden, so a popup can never be
  silently missing while the queue waits for it.

---

**How fast a popup appears:** the watcher waits until the new file has stopped
growing **and** is no longer locked, then opens the popup — that wait is
**1 second** by default (`"popup_delay_seconds"` in `config.json`, clamped to
0-10s; it was 3s before 0.6.42), and the UI checks its popup queue every 50ms,
so a finished scan is on screen about a second after the scanner stops writing.
The lock check is what protects a download that is still in progress: a browser
writing a file holds it open, so it never pops up early. Raise
`popup_delay_seconds` on a machine whose scanner writes in slow bursts, or set
it to `0` for the snappiest popups.

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

## OCR setup (DeepSeek V4.1 Flash)

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
     "ocr_model": "deepseek-v4.1-flash",
     "ocr_api_base": "https://opencode.ai/zen/go/v1",
     "ocr_thinking": "off"
   }
   ```

   `ocr_thinking` is the speed knob (`"off"` is the default and the fastest —
   see [Speed](#ocr-setup-deepseek-v41-flash) below; `"low"`/`"high"`/`"max"`
   force graded thinking, `"default"` sends nothing). It is **local-only**,
   like `ocr_model` and `ocr_api_base`: never synced from GitHub, never pushed
   back. `enable_ocr` is a **local-only** flag: it is never synced from the GitHub
   config and never pushed back, because OCR needs this machine's own key.
   The model/endpoint defaults above can be overridden per machine — a value
   that is only an *old default* of the app (e.g.
   `deepseek-v4-flash-vision-exp`) is upgraded to the current model
   automatically, so an existing `config.json` switches over by itself.

The **first** file — the one whose popup opens first — is read on its own, so
the popup you are looking at gets the whole gateway and fills in fastest. The
rest of the batch is sent together the moment that read finishes (or after
20 s if it is slow), so they are still read **simultaneously** (up to 8
vision calls at once) while you work through the queue. A popup whose file
has not been sent yet reads it immediately, so the file on screen never
waits. While a read is in flight the popup shows
`OCR: reading document… (N files read together)`.

**What the model reads, and what the app matches:** the model is asked to
**copy the printed values** — nothing else. The catalog is deliberately *not*
part of the prompt (it used to list every known site and client and order the
model to "output the Known Site name exactly as listed", which turned reading
one printed line into a fuzzy pick from a long list of near-duplicate names:
a note whose *Other References* said `Lodha Kharadi T-2` could come back as a
different real site, `Lodha Sital Baug`). The app resolves names itself, with
rules that are deterministic and testable: case, spacing and punctuation,
articles, one-letter variants, one extra word (a brand prefix), and a trailing
tower/wing/phase designator — `sital baug` → `Lodha Sital Baug`,
`Lodha Kharadi T-2` → `Lodha Kharadi`, `Larsen and Toubro` → `Larsen & Toubro`.
The designator is recognised in **every** spelling vendors and OCR use,
including the one where the dash comes first — `Lodha Wood-T6`,
`Lodha Wood T6`, `Lodha Wood T-6`, `Lodha Wood-T-6` are all `Lodha Wood`
(that spelling used to keep its T6), and inside brackets — `L & T (T-10)` and
`L & T (T-A)` are `L & T`. Designators are also **chained**: a tower plus a
unit type is one designator, so `Lodha Nibm-T6 Pent House` (and `Penthouse`,
`Lodha Nibm - T6 - Pent House`) is `Lodha Nibm`, not a site called
"Lodha Nibm-T6 Pent". While a bare trailing letter is still part of the
name (`Site A` is never `Site B`), `Parc-V` stays `Parc-V` and a bracket
holding a real name is left alone (`Acme Ozobe(Bellavista)`, `L & T (Retail)`).
A bracketed tower is only dropped when the value as printed matches nothing:
`Raheja Solaris (Tower-A)` and `Raheja Solaris (Tower-B)` are two different
sites, so a Tower-B note keeps its B. When several catalog sites are near-same
the **closest** one wins — fewest extra words first, then an identical name
over one that only matches because numbers are ignored, then fewest one-letter
differences — so `Lodha Wood` resolves to `LODHA - WOOD-kandivali` (the same
words) and not to `Lodha Woods Club House` (one letter *and* one word away),
whatever order the catalog happens to be in, and a catalog holding both
`Client 1` and `Client 2` answers `Client 2` with `Client 2`.
A name that matches nothing stays exactly as printed so you can review it (and
optionally *Add* it) before saving. The Serial Number is read from the
**Delivery Note No.** field (e.g. `RS/DC/26-27/6` → `6`) and, when OCR can't
read it, is back-filled from the file name (`RS-DC-26-27-6.pdf` → `6`). The
key never lands in `config.json`, so it can't leak to the public repo.

**A new document at an old file name is read again.** The OCR cache is keyed by
the file *and its content identity* (size + modified time), not by the path
alone. That matters because a scanner (or a download) re-uses the same name —
`dc.pdf`, say — once the app has moved the previous file away, and a cache
keyed by path alone handed the new document the **previous** file's read: the
popup filled itself with the last file's company, client, site and materials,
with no API call at all. Now the same untouched file is still served from
cache (no wasted reads during a batch) while a new file at that path is read
afresh.

**Nothing you typed is ever overwritten — and `↻ Retry OCR` really retries.**
The popup remembers which values *it* filled. A new read replaces those (so a
wrong site or material can actually be corrected), while anything you typed,
picked or toggled by hand is left alone — the status line says which
(`OCR: done in 2.9s — kept your site`). A retry also reads **with thinking
on** rather than repeating the identical fast call, because "the first answer
was wrong" is exactly when a more careful look is wanted. If a retry fails, the
values the rejected read had filled are cleared instead of staying behind
looking like your data. The log traces each read to the field it produced:

```
[ocr] RS-DC-26-27-6.pdf: company='Ruby Steel' client='Cowtown Infotech Services Limited' site='Lodha Kharadi T-2' serial='6' goods='Aluminium Section'
[filepicker] OCR applied for RS-DC-26-27-6.pdf: site 'Lodha Kharadi T-2' -> 'Lodha Kharadi'; materials ['Aluminium']
```

**Speed (`ocr_thinking`):** a read is dominated by how long the model
*thinks* before answering — not by the upload, the image or the prompt.
Thinking mode is **on by default** on the DeepSeek API (effort `high`), and
`reasoning_effort` only *grades* it down: a delivery note that needs five
fields copied out was spending thousands of reasoning tokens on a chain of
thought first, which is where the 10-60 seconds per file went (and why reads
were so variable). OCR is transcription, not reasoning, so every read now asks
for thinking to be **off**:

1. `"reasoning_effort": "none"` — verified live against the OpenCode Go
   gateway by another client ([opencode#27555](https://github.com/anomalyco/opencode/issues/27555));
2. `{"thinking": {"type": "disabled"}}` — DeepSeek's documented OpenAI-format
   toggle ([thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/));
3. `"reasoning_effort": "low"` — graded thinking (the old default);
4. nothing at all — the model's own default (the slow one).

The app walks that ladder, and — crucially — does **not** trust a `200 OK`: if
the reply still contains reasoning tokens, that rung is dropped for the rest of
the run. Any failure of a rung that carries a thinking field (4xx, 5xx, even a
connection error) drops it and tries the next one, so the ladder always ends
with exactly the request the app sent before thinking control existed: an
endpoint that refuses a field costs one extra round trip, never a broken or a
slow read. Which rung a read ended up using is in its log line
(`mode=…`). A thinking-free read that cannot produce the table is
re-read **once with thinking on** instead of returning nothing, so speed never
costs accuracy. Set `"ocr_thinking"` in `config.json` to `"low"`/`"high"`/
`"max"` to force graded thinking, or to `"default"` to send no thinking field
at all (the old `ocr_reasoning_effort` key still works). `↻ Retry OCR` always
reads with thinking on (`"low"`, or your configured level when that is
already graded), so a hard document can be re-read carefully without making
every automatic read slow. Each read logs every
stage — total, render, image size, attempts, mode, tokens and how many of them
were reasoning:

```
[ocr] RS-DC-26-27-6.pdf: read in 3.1s, render 0.2s, image 0.36MB, mode=reasoning_effort=none, 214 tokens
```

The popup shows the seconds too, counting up live while it waits
(`OCR: reading document… 12s`) and reporting the total when the fields land
(`OCR: fields filled in 3.1s — check before saving`). A read is also bounded:
45s per attempt, 100s for the whole read including retries, `Retry-After` is
honoured (capped at 15s), and an attempt that already took 20s+ is never
retried — so a stalled gateway can no longer hold a popup for minutes.

**Materials from the goods table:** the OCR transcribes the bold item
headings of the "Description of Goods" table, and **each heading selects at
most ONE material** — the most specific match in that line. A line reading
`SS Spigot` therefore selects the *fitting* (the mapped word, longer and more
specific) and **not** Stainless Steel as well; comma-, newline-, semicolon-,
pipe- or bullet-separated headings each contribute their own single material.
A new read replaces the materials the *previous* read selected (that is how a
retry fixes a wrong list); materials you picked or unticked yourself are kept,
and one you unticked is never put back.

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

**Auto-start at Windows login:** the app **registers itself at every launch**
and repairs a missing or stale entry — two independent mechanisms, because on
a real machine one of them is always unavailable for some reason:

- the per-user **Run key**
  (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`) — written with
  `winreg`, so it needs no PowerShell, no COM and no child process. This is the
  primary one;
- the classic **Startup-folder shortcut** (`shell:startup`), which you can see
  and delete yourself.

A mechanism only counts when it points at the *currently running* app and that
file still exists, so an update that replaces the .exe can never leave a
startup entry pointing at a dead path. The entry always names
**`FilePicker.exe` itself**: in a compiled build `sys.executable` is *not* the
app — Nuitka reports `<install folder>\python.exe`, a file the release does not
contain — and registering that phantom path is exactly how auto-start ended up
doing nothing at login while the log still claimed an entry was present. The
target is resolved the same way the updater resolves it, nothing is ever
registered for a file that is not there, and both writes are read back before
they are reported as installed. Windows' own **Task Manager → Startup apps**
switch is honoured too: if that entry is switched off, the log says so
(`switched OFF in Task Manager -> Startup apps`) and tells you to turn it back
on there — the app never flips it behind your back. If registration fails, the
log says so explicitly, with the reason (`[filepicker] auto-start FAILED …`)
instead of failing silently. Control it from the tray (**Auto-start at login:
On/Off**) or:

- Disable auto-start: set `"auto_start": false` in `config.json` (or use the tray).
- Manual control: `FilePicker.exe --install-startup` / `FilePicker.exe --remove-startup`
  / `FilePicker.exe --check-startup`.
- Manual alternative: press `Win+R`, type `shell:startup`, and drop a shortcut
  to `FilePicker.exe` in the folder that opens.

At login the app also **waits for the watch folder** (usually a mapped network
drive like `Z:\Unsorted`) to become available: Windows starts auto-start
programs *before* it reconnects mapped drives, so it retries for up to 30
minutes and starts watching the moment the drive appears — instead of crashing
at startup and looking like "auto-start doesn't work".

## Uninstalling

FilePicker is **portable** — there is no installer. It keeps everything in its
own folder; the only thing it writes elsewhere is the per-user Run key entry
(and the Startup shortcut) it registers so it can start with Windows, both
removable from the tray or with `--remove-startup`. To remove it completely:

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
- **A leftover `.old` can never block an update:** the running exe is moved
  aside with `os.replace` (which overwrites a stale `FilePicker.exe.old`
  atomically) and, when that leftover is locked by something else (antivirus,
  a dying process), the swap falls back to a free name
  (`FilePicker.exe.old2`, …) instead of failing. Older builds reported
  `WinError 183: Cannot create a file when that file already exists` here and
  stayed on their old version forever.
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
├── ocr.py           # OCR auto-fill (OpenCode Go DeepSeek V4.1 Flash)
├── winfocus.py      # Windows foreground/focus claim for popups & dialogs
├── viewer.py        # lightweight PDF / image / Excel preview window
├── filename.py      # filename formatting & collision resolution
├── organizer.py     # directory routing & file distribution
├── updater.py       # GitHub Releases auto-update (check + atomic swap)
├── tray.py          # system tray icon + auto-start / updates / Quit menu
├── startup.py       # Windows auto-start (Run key + Startup shortcut)
├── setup.py         # one-time first-run setup dialog (watch/root folders)
├── version.py       # app version
├── build.py         # Nuitka build script
├── build.bat        # Windows build shortcut
├── config.json      # persistent configuration
├── requirements.txt
└── .github/workflows/build.yml   # CI: build + release on every commit
```