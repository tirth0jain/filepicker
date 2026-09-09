"""Configuration management for FilePicker.

Loads and persists a ``config.json`` file. The config is written next to the
application (the directory containing this module) so it travels with the
utility and survives reinstalls. All dynamic changes made from the popup UI
(companies, clients, sites, materials, doc types) are saved back to this file.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from ocr import OCR_API_BASE, OCR_MODEL

# Remote live config — single source of truth for clients/sites.
# Every popup fetches this so all users see the same data instantly.
GITHUB_CONFIG_URL = "https://raw.githubusercontent.com/tirth0jain/filepicker/main/config.json"

# GitHub API details for pushing local additions (Add Site/Company) back to
# the repo so every machine sees them without a manual git push.
GITHUB_REPO = "tirth0jain/filepicker"
GITHUB_BRANCH = "main"
GITHUB_PATH = "config.json"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"

# Default configuration used the very first time the app runs.
DEFAULT_CONFIG: Dict[str, Any] = {
    "watch_directory": str(Path.home() / "Downloads"),
    "root_directory": "D:/Company_Data",
    "doc_types": ["DC", "Tax Invoice", "Purchase Order", "MTC"],
    "materials": {
        "Aluminium": "AL",
        "Carbon": "CA",
        "Stainless Steel": "SS",
        "Mild Steel": "MS",
        "Galvanized Iron": "GI",
    },
    # NOTE: material codes are exactly two letters (AL, SS, ...) and are
    # suffixed with "1" at filename time (AL -> AL1, SS -> SS1) so the tag
    # is never a bare letter.
    # Top-level company names (shown as a dropdown; the first is the default).
    "companies": ["Company A", "Company B"],
    # Optional per-company initials used in filenames (e.g. "Ruby Steel": "RS").
    # Companies not listed here fall back to auto-derived initials.
    "company_initials": {},
    # Each client owns a list of sites.
    "clients": {
        "Alpha Infra": ["Site 1 - Mumbai", "Site 2 - Pune"],
        "Beta Projects": ["Plant Central"],
    },
    # Register a Startup-folder shortcut on first run so the app launches
    # automatically at Windows login. Set to false to disable.
    "auto_start": True,
    # Live GitHub config sync — when true the app polls
    # raw.githubusercontent.com every 30s (and on every popup open) so a
    # push to config.json on GitHub appears for all users without rebuilding
    # the exe. Set to false to use only the local config.json.
    "enable_live_config": True,
    # When true, any "Add Site / Add Company / Add Material" action also
    # pushes the updated config.json back to GitHub (requires a token — see
    # GITHUB_TOKEN below). This is how a site added on one machine appears
    # for every other machine within 30s without a manual git push.
    # Requires a fine-grained PAT with Contents: read & write on this repo.
    # The token is NEVER stored in config.json — it lives in
    # `github_token.txt` next to the exe (or env FILEPICKER_GITHUB_TOKEN).
    "enable_github_push": True,
    # OCR auto-fill of the popup (Company/Client/Site) using the OpenCode Go
    # "DeepSeek V4 Flash Vision Exp" model. LOCAL-ONLY toggle: it is never
    # synced from the GitHub config nor pushed back, because OCR needs this
    # machine's own API key (see _read_opencode_token) and one machine
    # enabling it must not force it on every install.
    "enable_ocr": True,
    # Open the file preview automatically with every popup. Set to false to
    # start with the metadata form only (Ctrl+P / the Preview button still
    # toggles it).
    "preview_open_by_default": True,
    # While a popup is open and Alt is held, Alt+<key> combinations are
    # swallowed system-wide so NO other program reacts to them (AutoDesk
    # apps fire on Alt+letters); the popup itself still receives every chord.
    # Set to false to let other programs see Alt normally.
    "block_alt_for_other_apps": True,
    # Vision model + endpoint used by the OCR feature (OpenCode Go catalog,
    # OpenAI-compatible API). Overridable per machine in config.json.
    "ocr_model": OCR_MODEL,
    "ocr_api_base": OCR_API_BASE,
}


def default_config_path() -> Path:
    """Return the path to the config.json file next to the app.

    When frozen (Nuitka standalone) the modules live inside the app folder, but
    ``__file__`` can point at a temporary/embedded location; the config file
    must always be found next to the running executable so the user's data is
    read (and new files are created there).
    """
    if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent / "config.json"


def default_token_path() -> Path:
    """Path to the file that holds the GitHub PAT for pushing config.json.

    The token is deliberately NOT stored in config.json — otherwise it would
    be pushed to the public repo when the config is synced. Store it in
    `github_token.txt` next to the exe (or set env FILEPICKER_GITHUB_TOKEN).
    """
    if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)):
        return Path(sys.executable).resolve().parent / "github_token.txt"
    return Path(__file__).resolve().parent / "github_token.txt"


def default_opencode_token_path() -> Path:
    """Path to the file that holds the OpenCode Go API key used by OCR.

    Mirrors ``github_token.txt``: `opencode_token.txt` next to the exe (or
    env FILEPICKER_OPENCODE_TOKEN / OPENCODE_API_KEY). The key is the same
    one the opencode CLI uses (see `opencode auth`), and it is deliberately
    NEVER stored in config.json — otherwise it would be pushed to the public
    repo when the config is synced.
    """
    if getattr(sys, "frozen", False) or bool(getattr(sys, "nuitka_standalone", False)):
        return Path(sys.executable).resolve().parent / "opencode_token.txt"
    return Path(__file__).resolve().parent / "opencode_token.txt"


def _read_opencode_token() -> Optional[str]:
    """Return the OpenCode Go API key used by the OCR feature, else None.

    Order: env FILEPICKER_OPENCODE_TOKEN → env OPENCODE_API_KEY →
    opencode_token.txt (first line, `token = xyz` or bare) → opencode's own
    auth store as a dev convenience (~/.local/share/opencode/auth.json,
    providers "opencode-go" then "opencode").
    """
    for env_key in ("FILEPICKER_OPENCODE_TOKEN", "OPENCODE_API_KEY"):
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
    try:
        token_path = default_opencode_token_path()
        if token_path.exists():
            text = token_path.read_text(encoding="utf-8").strip()
            # Support file with `token = xyz` or just `xyz`
            if "=" in text:
                text = text.split("=", 1)[1].strip().strip('"\' ')
            return text or None
    except OSError:
        pass
    # Dev fallback: reuse the key the user pasted into `opencode auth`.
    try:
        auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        if auth.exists():
            data = json.loads(auth.read_text(encoding="utf-8"))
            for provider in ("opencode-go", "opencode"):
                entry = data.get(provider)
                if isinstance(entry, dict) and entry.get("key"):
                    return str(entry["key"]).strip() or None
    except Exception:
        pass
    return None


def _read_github_token() -> Optional[str]:
    """Return the GitHub PAT if configured, else None.

    Order: env FILEPICKER_GITHUB_TOKEN → env GITHUB_TOKEN → github_token.txt
    The file should contain just the token on the first line (no JSON).
    """
    for env_key in ("FILEPICKER_GITHUB_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(env_key, "").strip()
        if token:
            return token
    try:
        token_path = default_token_path()
        if token_path.exists():
            text = token_path.read_text(encoding="utf-8").strip()
            # Support file with `token = xyz` or just `xyz`
            if "=" in text:
                text = text.split("=", 1)[1].strip().strip('"\' ')
            return text or None
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# Name near-match ("almost or near same, never merely similar")
# --------------------------------------------------------------------------
# Used for both SITES and CLIENTS: the exact same rules dedupe a client
# written slightly differently ("Larsen and Toubro" vs "Larsen & Toubro").
# Articles that never tell two names apart ("The Sital Baug" == "Sital Baug").
# Only a LEADING article is dropped: a trailing "a"/"an"/"the" token is a
# real designation letter ("Kalpataru Vivant (T-A)", "Site A") that must be
# kept — "Site A" is NOT "Site B".
_SITE_ARTICLES = {"a", "an", "the"}


def normalize_site_name(name) -> str:
    """Fold a site name into its comparable form.

    Lowercases, turns every non-alphanumeric run (punctuation, spacing,
    brackets, "&") into a single space, and drops a leading article
    (a/an/the) — so "The LODHA Shital-Baug" and "Lodha shital baug" compare
    equal, while "Site A" and "Kalpataru Vivant (T-A)" keep their letters.
    """
    text = re.sub(r"[^0-9a-z]+", " ", str(name).lower())
    tokens = [t for t in text.split() if t]
    if tokens and tokens[0] in _SITE_ARTICLES:
        tokens = tokens[1:]
    return " ".join(tokens)


def _levenshtein(a: str, b: str) -> int:
    """Edit distance between two short strings (small, O(n*m) is fine)."""
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, ch_b in enumerate(b, 1):
        cur = [j]
        for i, ch_a in enumerate(a, 1):
            cur.append(min(prev[i] + 1, cur[-1] + 1, prev[i - 1] + (ch_a != ch_b)))
        prev = cur
    return prev[-1]


def _site_tokens_near(a: str, b: str) -> bool:
    """True when two (already normalized) words are the same word.

    Numbers are "don't care": "T1"/"T2", "Tower 1"/"Tower 2" and "Site A1"/
    "Site A2" are the same site — vendors and OCR write the numeral
    inconsistently, so it never decides a match. Otherwise allows exactly one
    wrong/extra/missing letter ("shital" vs "sital", "bag" vs "baug") and
    nothing looser — this is *near*, not similar.
    """
    if a == b:
        return True
    if a.isdigit() and b.isdigit():
        return True
    # Compare digit-stripped forms with the same one-letter tolerance:
    # "t1" vs "t2" -> "t" == "t"; "block3" vs "block4" -> "block" == "block".
    a_letters = re.sub(r"\d", "", a)
    b_letters = re.sub(r"\d", "", b)
    if a_letters and b_letters:
        if a_letters == b_letters:
            return True
        if len(a_letters) >= 2 and len(b_letters) >= 2 \
                and _levenshtein(a_letters, b_letters) <= 1:
            return True
    if not a or not b:
        return False
    if len(a) < 2 or len(b) < 2:
        return False
    return _levenshtein(a, b) <= 1


def _token_subsequence_matches(seq: List[str], sub: List[str]) -> bool:
    """True when every token of *sub* occurs in *seq* in order, near-identical.

    The lists may differ by at most one token (e.g. a brand prefix:
    "Lodha Shital Baug" vs "sital baug" is the same site, so the single
    extra word is tolerated). Every token of the shorter list must match a
    token of the longer one within one letter.
    """
    if len(sub) > len(seq):
        seq, sub = sub, seq
    if abs(len(seq) - len(sub)) > 1:
        return False
    i = 0
    for tok in sub:
        while i < len(seq) and not _site_tokens_near(tok, seq[i]):
            i += 1
        if i >= len(seq):
            return False
        i += 1
    return True


# Unit-designator words: a trailing "word + number" pair whose word is one
# of these is the same place as the name without the pair ("Kalpataru Elitus
# Tower 2" == "Kalpataru Elitus", "Lodha Regalia Phase 2" == "Lodha Regalia").
# Only ONE such pair is ever stripped, and only when real words remain —
# arbitrary words are never dropped ("Sital Baug 2" keeps "Baug").
_UNIT_WORDS = {
    "tower", "phase", "unit", "block", "level", "wing", "podium", "floor",
    "house", "building", "annex", "annexe", "plot", "flat", "shop", "sector",
    "zone", "stage", "pod", "yard", "office", "centre", "center",
    "winga", "wingb", "wingc", "wingd",
}


def _strip_trailing_designator(tokens: List[str]) -> List[str]:
    """Drop ONE trailing unit designator: a standalone number, or an
    'unit word + number' pair ('Tower 2', 'Phase 3').

    "Kalpataru Elitus Tower 2" is the same place as "Kalpataru Elitus" — the
    trailing designator is something vendors and OCR write inconsistently,
    so it never decides a match. Single-letter designators are never dropped
    ('Site A', 'Kalpataru Vivant (T-A)') and the strip never reduces a name
    to nothing.
    """
    if len(tokens) < 2 or not tokens[-1].isdigit():
        return tokens
    out = tokens[:-1]  # a trailing number itself never decides a match
    # Drop a preceding *unit* word too, but only when the pair is followed
    # by at least two real words ("Sital Baug 2" keeps "Baug"; "Tower 2"
    # alone stays intact).
    if len(out) >= 2 and out[-1].isalpha() and out[-1] in _UNIT_WORDS:
        out = out[:-1]
    return out or tokens


def _match_forms(name: str) -> tuple:
    """The (normalized, designator-stripped) comparable forms of a name."""
    norm = normalize_site_name(name)
    stripped = " ".join(_strip_trailing_designator(norm.split()))
    return norm, stripped


def find_near_name(existing_names, candidate) -> Optional[str]:
    """The existing catalog name that is the *same place* as ``candidate``.

    Applies to site and client names alike. Matching ignores case,
    punctuation, spacing, articles (a/an/the) and numbers — "T1"/"T2"/"Tower
    1"/"Tower 2" are the same site, so the exact numeral never blocks a
    match. A trailing unit designator is ignored too: "Kalpataru Elitus
    Tower 2" matches "Kalpataru Elitus" (and "Lodha Shital Baug Tower 2"
    matches "Sital Baug" — the brand prefix AND the designator are both
    tolerated). Tolerates one-letter spelling variants per word ("shital
    bag" vs "Sital Baug", "Larsen and Toubro" vs "Larsen & Toubro") and at
    most one extra word (brand prefixes like "Lodha"). Names that differ only
    in spacing/punctuation ("T-A" vs "TA" vs "T A") are equivalent.
    Deliberately strict: names that merely share words are NOT matched
    ("Sai Baug" is never "Sital Baug"), and single-letter tokens are
    exact-only ("Site A" is never "Site B"). Returns the canonical existing
    spelling.
    """
    cand_norm, cand_strip = _match_forms(candidate)
    if not cand_norm:
        return None
    cand_squeezed = cand_norm.replace(" ", "")
    cand_forms = [(cand_norm, cand_squeezed)]
    if cand_strip != cand_norm:
        cand_forms.append((cand_strip, cand_strip.replace(" ", "")))

    for name in existing_names:
        norm, strip = _match_forms(name)
        if not norm:
            continue
        name_forms = [(norm, norm.replace(" ", ""))]
        if strip != norm:
            name_forms.append((strip, strip.replace(" ", "")))
        # Either form pair may match (original, or both stripped of a
        # trailing unit designator) — same words with only
        # spacing/punctuation differences, or near-identical word lists.
        for (a, a_sq) in cand_forms:
            for (b, b_sq) in name_forms:
                if a == b or a_sq == b_sq:
                    return str(name)
                if _token_subsequence_matches(a.split(), b.split()):
                    return str(name)
    return None


def find_near_site(existing_sites, candidate) -> Optional[str]:
    """Compatibility alias of :func:`find_near_name` for site name lists."""
    return find_near_name(existing_sites, candidate)


class ConfigManager:
    """Thread-safe wrapper around the persistent config.json file.

    Reads the file lazily, caches the parsed structure in memory, and writes
    every mutation back to disk so the config is always up to date.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = deepcopy(DEFAULT_CONFIG)
        self._loaded = False
        # mtime of config.json when the app last wrote it (or first read it).
        # When the file changes on disk AFTER that (a hand edit — deleting
        # sites/companies/...), the auto-sync must not clobber the edit.
        self._last_write_mtime: Optional[float] = None
        # Config changes made from a popup (new sites/clients/materials/...)
        # that have NOT been pushed to GitHub yet. They are pushed only when
        # a file is actually saved (flush_pending_push) or when the user
        # force-pushes from the tray — never while a popup is still open.
        self._pending_push_reasons: List[str] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        """Load the config from disk (or defaults), merging any missing keys."""
        with self._lock:
            if not self._loaded:
                self._read_from_disk()
                self._loaded = True
            return self._data

    def _read_from_disk(self) -> None:
        if not self.path.exists():
            # No config yet: seed the file from DEFAULT_CONFIG (first run) so
            # the user has a file to edit, then treat that file as the source.
            self._data = deepcopy(DEFAULT_CONFIG)
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a JSON object")
            # config.json is the source of truth: use its contents as-is and
            # never merge config.py's placeholder defaults over them. Missing
            # keys are handled at the accessor level (each uses .get with a
            # safe fallback) without being written back.
            self._data = loaded
            # Baseline: the mtime of the app's last write (persisted), so a
            # hand edit made while the app was closed is still detected and
            # never clobbered by the auto-sync. Falls back to the current
            # file mtime when no marker exists yet.
            self._last_write_mtime = self._read_mtime_marker()
            if self._last_write_mtime is None:
                self._last_write_mtime = self._file_mtime(self.path)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            # Fall back to defaults but never crash the watcher.
            self._data = deepcopy(DEFAULT_CONFIG)
            print(f"[config] Could not read {self.path}: {exc}")

    @staticmethod
    def _file_mtime(path: Path) -> Optional[float]:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _mtime_marker_path(self) -> Path:
        """Sidecar holding the mtime of the last app write to config.json.

        Persisted so a hand edit survives app restarts (the updater relaunches
        the app often): without it, the startup/periodic sync would resurrect
        deleted entries as soon as the app restarted.
        """
        return self.path.with_name(self.path.name + ".mtime")

    def _read_mtime_marker(self) -> Optional[float]:
        try:
            return float(self._mtime_marker_path().read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _write_mtime_marker(self, value: Optional[float]) -> None:
        if value is None:
            return
        try:
            self._mtime_marker_path().write_text(f"{value}\n", encoding="utf-8")
        except OSError:
            pass

    def _file_edited_externally(self) -> bool:
        """True when config.json changed on disk after the app last wrote it.

        A hand edit (deleting sites/companies/doc types/materials from the
        file, changing paths...) must never be silently overwritten by the
        periodic GitHub union-sync — otherwise the deleted entries come back
        before they can be pushed. The tray force-push publishes the hand
        edited file; the next app write (e.g. Add Site) resumes normal
        syncing. The 1s epsilon absorbs filesystem timestamp coarseness.
        """
        last = getattr(self, "_last_write_mtime", None)
        if last is None:
            return False
        now = self._file_mtime(self.path)
        return now is not None and now > last + 1.0

    def reload(self) -> Dict[str, Any]:
        """Force a reload from disk (e.g. after external edits)."""
        with self._lock:
            self._loaded = False
            return self.load()

    # ------------------------------------------------------------------
    # Live GitHub config (single source of truth for all users)
    # ------------------------------------------------------------------
    def fetch_github_config(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """Fetch the live config from GitHub. Returns None on failure."""
        try:
            import urllib.request
            import time as _time

            url = GITHUB_CONFIG_URL
            # Bust raw.githubusercontent CDN cache (5 min) so a push shows up
            # within one poll interval instead of waiting for CDN expiry.
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}_t={int(_time.time())}"
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "FilePicker",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and "clients" in data:
                return data
        except Exception as exc:
            print(f"[config] GitHub live config fetch failed: {exc}")
        return None

    @property
    def enable_live_config(self) -> bool:
        """Whether to poll GitHub for live config. Local-only flag, never overwritten by remote."""
        return bool(self.load().get("enable_live_config", True))

    def sync_from_github(self, timeout: float = 5.0) -> bool:
        """Fetch and apply the live config if it changed. Returns True if updated."""
        if not self.enable_live_config:
            return False
        remote = self.fetch_github_config(timeout=timeout)
        if remote is None:
            return False
        return self.apply_github_config(remote)

    def force_sync_from_github(self, timeout: float = 10.0) -> Optional[bool]:
        """Manual "Force sync" — make the local catalog match GitHub exactly,
        deletions included.

        The automatic syncs union-merge so a site added locally is never lost,
        but that also means entries deleted on GitHub stay forever on every
        machine. This tray-triggered option instead REPLACES the shared
        catalog keys (companies, company_initials, clients, materials,
        doc_types) with the remote values, so a site/client/material deleted
        on the repo disappears locally too. Local-only keys stay untouched
        (watch_directory, root_directory, enable_ocr, ocr_model,
        ocr_api_base).

        Returns None when the fetch failed, True when the local config was
        overwritten/saved, False when it already matched the repo.
        """
        if not self.enable_live_config:
            return False
        remote = self.fetch_github_config(timeout=timeout)
        if remote is None:
            return None
        with self._lock:
            changed = False
            for key in ("companies", "company_initials", "clients", "materials", "doc_types"):
                if key in remote and remote[key] != self._data.get(key):
                    self._data[key] = deepcopy(remote[key])
                    changed = True
            for key in ("enable_live_config", "enable_github_push", "auto_start"):
                if key in remote and remote[key] != self._data.get(key):
                    self._data[key] = remote[key]
                    changed = True
            if changed:
                print("[config] Force sync: local catalog replaced with repo values")
                self.save()
            return changed

    def apply_github_config(self, remote: Dict[str, Any]) -> bool:
        """Merge the live GitHub config into the local one.

        Only the shared catalog keys are merged (companies, clients,
        materials, doc_types, company_initials) — a union so a site added
        locally that hasn't yet been pushed to GitHub is **not** deleted when
        the next poll fetches the still-old remote. Local paths
        (watch_directory, root_directory) are never clobbered. The live-sync
        flags (`enable_live_config`, `enable_github_push`) are also synced
        from remote so a repo change propagates to all installs.

        When config.json was edited by hand since the app last wrote it, the
        merge is SKIPPED (returns False): applying the union would resurrect
        entries the user just deleted. The tray "Push local config to
        GitHub" publishes the hand-edited file instead.

        Returns True if anything changed and was saved.
        """
        with self._lock:
            if self._file_edited_externally():
                print("[config] config.json was edited outside the app — "
                      "skipping this auto-sync round so the edit is not "
                      "overwritten (tray → \"Push local config to GitHub\" "
                      "publishes the edited file)")
                return False
            # Union-merge catalog so concurrent local adds are not lost
            # when the remote is still stale (the bug that made Add Site
            # disappear when you moved to the next field).
            merged = self._merge_for_push(remote, self._data)
            changed = False
            for key in ("companies", "company_initials", "clients", "materials", "doc_types"):
                if key in merged and merged[key] != self._data.get(key):
                    self._data[key] = merged[key]
                    changed = True
            for key in ("enable_live_config", "enable_github_push", "auto_start"):
                if key in remote and remote[key] != self._data.get(key):
                    self._data[key] = remote[key]
                    changed = True
            if changed:
                self.save()
            return changed

    @property
    def enable_github_push(self) -> bool:
        """Whether manual additions should be pushed back to GitHub.

        Requires a PAT in `github_token.txt` or env FILEPICKER_GITHUB_TOKEN.
        If the key is missing (old installs) it defaults to *enabled* when a
        token is present, so placing the token file is enough.
        """
        val = self.load().get("enable_github_push", None)
        if val is None:
            # Old config without the flag — enable automatically when token exists
            return bool(_read_github_token()) and self.enable_live_config
        return bool(val)

    def _github_push_enabled(self) -> bool:
        if not self.enable_live_config:
            return False
        # Token must exist; flag may be missing (old config) — handled above
        if not _read_github_token():
            return False
        return self.enable_github_push

    # ------------------------------------------------------------------
    # Push local additions back to GitHub (so every machine sees them)
    # ------------------------------------------------------------------
    def push_to_github(
        self,
        reason: str = "FilePicker: update config",
        timeout: float = 10.0,
    ) -> bool:
        """Push the local catalog back to GitHub.

        Runs after a file was successfully saved (flush_pending_push), via
        the tray's manual force push (force_push_to_github), or the
        ``--push-config`` CLI. Merges the local catalog (companies, clients,
        etc.) into the current GitHub file so concurrent edits from two
        machines are unioned, not lost. Returns True on success.

        The token is read from `FILEPICKER_GITHUB_TOKEN` / `GITHUB_TOKEN` /
        `github_token.txt` — it is NEVER stored in config.json.
        """
        if not self._github_push_enabled():
            if self.enable_github_push and not _read_github_token():
                print("[config] enable_github_push is true but no token found (env FILEPICKER_GITHUB_TOKEN or github_token.txt). Skipping push.")
            return False

        token = _read_github_token()
        if not token:
            return False

        # Snapshot local catalog under lock
        with self._lock:
            local_data = deepcopy(self._data)

        try:
            import urllib.request
            import urllib.error

            api_url = GITHUB_API_URL

            # 1. GET current file to obtain sha + remote content
            sha: Optional[str] = None
            remote_data: Dict[str, Any] = {}
            try:
                req = urllib.request.Request(
                    f"{api_url}?ref={GITHUB_BRANCH}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "FilePicker",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                    sha = info.get("sha")
                    content_b64 = info.get("content", "")
                    if content_b64:
                        # GitHub returns base64 with newlines
                        decoded = base64.b64decode(content_b64).decode("utf-8")
                        remote_data = json.loads(decoded)
                        if not isinstance(remote_data, dict):
                            remote_data = {}
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # File doesn't exist yet — will be created
                    sha = None
                    remote_data = {}
                else:
                    print(f"[config] GitHub GET failed ({e.code}): {e.reason}")
                    return False
            except Exception as exc:
                print(f"[config] GitHub GET failed: {exc}")
                return False

            # 2. Merge local catalog into remote (union, not overwrite)
            merged = self._merge_for_push(remote_data, local_data)
            # If nothing to push (remote already has our catalog), skip
            # Compare only the catalog keys for cheap equality
            catalog_keys = ("companies", "company_initials", "clients", "materials", "doc_types")
            if all(merged.get(k) == remote_data.get(k) for k in catalog_keys):
                # For a brand-new file (remote_data empty) this is never true
                if remote_data:
                    return False

            # Build new file content: start from remote_data (preserves remote's
            # watch_directory etc.) and replace catalog keys with merged.
            # If remote was empty, start from local_data but keep merged catalog.
            if remote_data:
                new_content = deepcopy(remote_data)
            else:
                new_content = deepcopy(local_data)
            for k in catalog_keys:
                if k in merged:
                    new_content[k] = merged[k]

            new_json = json.dumps(new_content, indent=2, ensure_ascii=False) + "\n"
            b64_content = base64.b64encode(new_json.encode("utf-8")).decode("ascii")

            payload: Dict[str, Any] = {
                "message": reason,
                "content": b64_content,
                "branch": GITHUB_BRANCH,
            }
            if sha:
                payload["sha"] = sha

            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "FilePicker",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 201):
                    print(f"[config] Pushed config to GitHub ({reason})")
                    return True
                print(f"[config] GitHub PUT unexpected status {resp.status}")
                return False

        except urllib.error.HTTPError as e:
            # 409 = sha mismatch (concurrent edit) — fetch + retry once
            if e.code == 409:
                print("[config] GitHub push conflict (409) — retrying with merged remote…")
                try:
                    # Simple retry: fetch again and re-merge once
                    return self.push_to_github(reason=reason, timeout=timeout)
                except RecursionError:
                    pass
            try:
               detail = e.read().decode("utf-8", errors="ignore")[:500]
            except Exception:
                detail = str(e)
            print(f"[config] GitHub push failed ({e.code}): {detail}")
            return False
        except Exception as exc:
            print(f"[config] GitHub push failed: {exc}")
            return False

    def _push_async(self, reason: str = "FilePicker: update config") -> None:
        """Fire-and-forget push on a daemon thread (never blocks the UI)."""
        if not self._github_push_enabled():
            return

        def _work() -> None:
            try:
                self.push_to_github(reason=reason)
            except Exception as exc:
                print(f"[config] async push error: {exc}")

        threading.Thread(target=_work, name="filepicker-github-push", daemon=True).start()

    # ------------------------------------------------------------------
    # Deferred pushes — "no config push unless a file is saved"
    # ------------------------------------------------------------------
    def _mark_push_pending(self, reason: str) -> None:
        """Record a catalog change to be pushed to GitHub only after a save.

        Add Site/Client/Company/Material/Doc Type always write to the local
        config.json immediately (so the current file and folder path are
        right), but the GitHub push is deferred: it happens only when a file
        is actually SAVED (see :meth:`flush_pending_push`) or when the user
        force-pushes from the tray. A half-filled popup that is skipped (or
        never saved) never touches the repo.
        """
        with self._lock:
            if reason not in self._pending_push_reasons \
                    and len(self._pending_push_reasons) < 8:
                self._pending_push_reasons.append(reason)

    def flush_pending_push(self, force_reason: Optional[str] = None) -> bool:
        """Push every pending catalog change (called after a successful save).

        Returns True when a push was started, False when there was nothing
        pending. The push itself runs on a daemon thread and never blocks.
        """
        with self._lock:
            if not self._pending_push_reasons:
                return False
            reasons = list(self._pending_push_reasons)
            self._pending_push_reasons = []
        combined = "; ".join(reasons)
        self._push_async(reason=force_reason or f"FilePicker: {combined}")
        return True

    def force_push_to_github(
        self,
        reason: str = "FilePicker: force push local config",
        timeout: float = 10.0,
    ) -> bool:
        """Replace the GitHub config.json with THIS machine's local file.

        The normal :meth:`push_to_github` union-merges (nothing is ever
        lost); this one instead DELETES what is on GitHub and writes the
        local config in its place — deletions included — which is exactly
        what the tray's "Push local config to GitHub" action should do when
        the local catalog is the one to publish. Any pending deferred pushes
        are superseded (the whole local file goes up anyway).

        The file is RELOADED from disk first, so hand edits — deleting
        sites/companies/doc types/materials from config.json — are what gets
        published, never a stale in-memory copy. A config without a usable
        catalog ("clients" missing or not an object) is REFUSED rather than
        pushed: an empty/broken file on GitHub would show "empty" everywhere
        and silently break every machine's live sync.
        """
        if not self._github_push_enabled():
            return False
        token = _read_github_token()
        if not token:
            return False
        with self._lock:
            # Reload config.json from disk: the user may have deleted
            # sites/companies/etc. by hand — that file is the truth.
            self.reload()
            local_data = deepcopy(self._data)
            self._pending_push_reasons = []
            # Accept this file state as the app's own, so the next
            # auto-sync resumes normally (remote == local after the push).
            self._last_write_mtime = self._file_mtime(self.path)
            self._write_mtime_marker(self._last_write_mtime)
            if not isinstance(local_data.get("clients"), dict):
                print("[config] FORCE PUSH REFUSED: local config.json has "
                      f"no \"clients\" catalog ({local_data.get('clients')!r}). "
                      "The file looks empty/broken — fix config.json and "
                      "retry, so GitHub is never replaced with an empty file.")
                return False
        try:
            import urllib.request
            import urllib.error

            api_url = GITHUB_API_URL

            # 1. GET current file to obtain sha (404 = not yet created).
            sha: Optional[str] = None
            try:
                req = urllib.request.Request(
                    f"{api_url}?ref={GITHUB_BRANCH}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "FilePicker",
                    },
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                    sha = info.get("sha")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print(f"[config] GitHub GET failed ({e.code}): {e.reason}")
                    return False
            except Exception as exc:
                print(f"[config] GitHub GET failed: {exc}")
                return False

            # 2. PUT the ENTIRE local file as-is (no merge, no union).
            new_json = json.dumps(local_data, indent=2, ensure_ascii=False) + "\n"
            b64_content = base64.b64encode(new_json.encode("utf-8")).decode("ascii")
            payload: Dict[str, Any] = {
                "message": reason,
                "content": b64_content,
                "branch": GITHUB_BRANCH,
            }
            if sha:
                payload["sha"] = sha

            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                api_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "FilePicker",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 201):
                    print(f"[config] FORCE-pushed local config to GitHub ({reason})")
                    return True
                print(f"[config] GitHub PUT unexpected status {resp.status}")
                return False
        except urllib.error.HTTPError as e:
            # 409 = sha mismatch (concurrent edit) — fetch + retry once
            if e.code == 409:
                print("[config] GitHub force push conflict (409) — retrying…")
                try:
                    return self.force_push_to_github(reason=reason, timeout=timeout)
                except RecursionError:
                    pass
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:500]
            except Exception:
                detail = str(e)
            print(f"[config] GitHub force push failed ({e.code}): {detail}")
            return False
        except Exception as exc:
            print(f"[config] GitHub force push failed: {exc}")
            return False

    @staticmethod
    def _merge_for_push(remote: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
        """Union remote + local catalog so concurrent adds are not lost."""
        merged: Dict[str, Any] = {}

        # companies — union, case-insensitive dedup, preserve order (remote first, then local additions)
        def _merge_list_str(rem: List[str], loc: List[str]) -> List[str]:
            seen = {str(x).strip().lower(): str(x) for x in rem if isinstance(x, str)}
            out = list(rem)
            for item in loc:
                if not isinstance(item, str):
                    continue
                key = item.strip().lower()
                if key not in seen:
                    out.append(item)
                    seen[key] = item
            return out

        rem_companies = list(remote.get("companies", []))
        loc_companies = list(local.get("companies", []))
        merged["companies"] = _merge_list_str(rem_companies, loc_companies)

        # company_initials — merge dicts, local wins on conflict (new override)
        rem_init = dict(remote.get("company_initials", {}))
        loc_init = dict(local.get("company_initials", {}))
        merged_init = dict(rem_init)
        merged_init.update({str(k): str(v) for k, v in loc_init.items()})
        merged["company_initials"] = merged_init

        # clients — union keys, and for each client union sites
        rem_clients = remote.get("clients", {})
        loc_clients = local.get("clients", {})
        if not isinstance(rem_clients, dict):
            rem_clients = {}
        if not isinstance(loc_clients, dict):
            loc_clients = {}
        # Map lower -> canonical key + sites
        # Build from remote first
        merged_clients: Dict[str, List[str]] = {}
        lower_to_key: Dict[str, str] = {}
        for k, v in rem_clients.items():
            key = str(k)
            lower_to_key[key.lower()] = key
            merged_clients[key] = list(v) if isinstance(v, list) else []

        for k, v in loc_clients.items():
            key = str(k)
            low = key.lower()
            canon = lower_to_key.get(low)
            if canon is None:
                # New client from local
                merged_clients[key] = list(v) if isinstance(v, list) else []
                lower_to_key[low] = key
            else:
                # Existing client — union sites
                loc_sites = list(v) if isinstance(v, list) else []
                merged_sites = merged_clients.get(canon, [])
                merged_clients[canon] = _merge_list_str(merged_sites, loc_sites)

        merged["clients"] = merged_clients

        # materials — dict union, local wins
        rem_mat = dict(remote.get("materials", {}))
        loc_mat = dict(local.get("materials", {}))
        merged_mat = dict(rem_mat)
        merged_mat.update({str(k): str(v) for k, v in loc_mat.items()})
        merged["materials"] = merged_mat

        # doc_types — union list
        rem_docs = list(remote.get("doc_types", []))
        loc_docs = list(local.get("doc_types", []))
        merged["doc_types"] = _merge_list_str(rem_docs, loc_docs)

        return merged

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self) -> None:
        """Write the current in-memory config to disk atomically."""
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
                self._last_write_mtime = self._file_mtime(self.path)
                self._write_mtime_marker(self._last_write_mtime)
            except OSError as exc:
                print(f"[config] Could not write {self.path}: {exc}")

    # ------------------------------------------------------------------
    # Typed accessors
    # ------------------------------------------------------------------
    @property
    def watch_directory(self) -> str:
        return str(self.load().get("watch_directory", ""))

    @property
    def root_directory(self) -> str:
        return str(self.load().get("root_directory", ""))

    @property
    def doc_types(self) -> List[str]:
        return list(self.load().get("doc_types", []))

    @property
    def materials(self) -> Dict[str, str]:
        """Return a copy of the {material name -> shortcode} mapping."""
        return dict(self.load().get("materials", {}))

    @property
    def companies(self) -> List[str]:
        """Return the list of top-level company names."""
        return list(self.load().get("companies", []))

    @property
    def company_initials(self) -> Dict[str, str]:
        """Return the {company name -> initials} override map."""
        initials = self.load().get("company_initials", {})
        return {str(name): str(code) for name, code in initials.items()}

    @property
    def clients(self) -> Dict[str, List[str]]:
        """Return a copy of the {client name -> [sites]} mapping."""
        clients = self.load().get("clients", {})
        return {name: list(sites) for name, sites in clients.items()}

    def sites_for(self, client: str) -> List[str]:
        return list(self._clients_dict().get(client.strip().lower(), []))

    def find_near_site(self, existing_sites, candidate) -> Optional[str]:
        """The existing site that is the same place as ``candidate``, else None.

        See :func:`find_near_name` — near-match, never merely similar:
        case/spacing/punctuation/articles/numbers ignored, one-letter
        variants and one extra word tolerated, single letters exact-only.
        """
        return find_near_site(existing_sites, candidate)

    def find_near(self, existing_names, candidate) -> Optional[str]:
        """The catalog name that is the same place as ``candidate``.

        The generic near-match (used for CLIENT names as well as sites — the
        rules are identical: numbers ignored, one-letter variants and one
        extra word tolerated, case/spacing/punctuation/articles don't
        matter). See :func:`find_near_name`.
        """
        return find_near_name(existing_names, candidate)

    def all_clients(self) -> List[str]:
        """Every client name (for the OCR known-clients list)."""
        return [str(k) for k in self.clients]

    def all_sites(self) -> List[str]:
        """Every site name across all clients (for the OCR known-sites list)."""
        out: List[str] = []
        for sites in self._clients_dict().values():
            for s in sites:
                name = str(s).strip()
                if name and name not in out:
                    out.append(name)
        return out

    def _clients_dict(self) -> Dict[str, List[str]]:
        """The raw {client -> [sites]} map with case-insensitive keys."""
        clients = self.load().get("clients", {})
        return {str(k).lower(): list(v) for k, v in clients.items()}

    @property
    def auto_start(self) -> bool:
        """Whether the app should register itself to launch at Windows login."""
        return bool(self.load().get("auto_start", True))

    @property
    def enable_ocr(self) -> bool:
        """Whether the popup auto-fills Company/Client/Site via OCR.

        Reads the LOCAL config only — like watch_directory/root_directory,
        this flag is never merged from the GitHub config nor pushed back, so
        one machine can enable OCR without forcing it on all installs.
        """
        return bool(self.load().get("enable_ocr", False))

    @property
    def ocr_model(self) -> str:
        """The vision model id used for OCR (OpenCode Go catalog)."""
        return str(self.load().get("ocr_model", OCR_MODEL))

    @property
    def ocr_api_base(self) -> str:
        """The OpenAI-compatible endpoint base used for OCR."""
        return str(self.load().get("ocr_api_base", OCR_API_BASE))

    @property
    def opencode_token(self) -> Optional[str]:
        """The OpenCode Go API key (env / opencode_token.txt / opencode auth store)."""
        return _read_opencode_token()

    @property
    def preview_open_by_default(self) -> bool:
        """Whether every popup opens with the file preview already shown."""
        return bool(self.load().get("preview_open_by_default", True))

    @property
    def block_alt_for_other_apps(self) -> bool:
        """Whether Alt+<key> is withheld from every other program while a
        popup is open (so AutoDesk-style apps never react to the popup's
        Alt material hotkeys)."""
        return bool(self.load().get("block_alt_for_other_apps", True))

    # ------------------------------------------------------------------
    # Mutators (each persists to disk)
    # ------------------------------------------------------------------
    def set_watch_directory(self, value: str) -> None:
        with self._lock:
            self.load()["watch_directory"] = value
            self.save()

    def set_root_directory(self, value: str) -> None:
        with self._lock:
            self.load()["root_directory"] = value
            self.save()

    def add_company(self, company: str) -> None:
        changed = False
        with self._lock:
            companies = self.load().setdefault("companies", [])
            if not self._ci_matches(companies, company):
                companies.append(company)
                self.save()
                changed = True
        if changed:
            self._mark_push_pending(reason=f"add company '{company}'")

    def add_client(self, client: str, sites: Optional[List[str]] = None) -> str:
        """Add a new client (optionally with its sites).

        Near-same clients are never duplicated: when ``client`` is the same
        place as an existing client (same words, ignoring case/spacing/
        punctuation/articles/numbers, at most one letter off per word and at
        most one extra word), the existing canonical spelling is returned and
        nothing is added. Returns the effective client name.
        """
        client = client.strip()
        if not client:
            return ""
        changed = False
        with self._lock:
            clients = self.load().setdefault("clients", {})
            canonical = find_near_name(list(clients.keys()), client)
            if canonical is not None:
                if canonical != client:
                    print(f"[config] client '{client}' is the same client as '{canonical}' — reusing existing name")
                return canonical
            clients[client] = list(sites or [])
            self.save()
            changed = True
        if changed:
            self._mark_push_pending(reason=f"add client '{client}'")
        return client

    def add_site(self, client: str, site: str) -> str:
        """Add a new site under ``client``; create the client if needed.

        Near-same entries are never duplicated — neither the client nor the
        site: when ``site`` is the same place as an existing site (same
        words, ignoring case/spacing/punctuation/articles/numbers, at most
        one letter off per word and at most one extra word like a brand
        prefix), the existing canonical spelling is returned and nothing is
        added; a client written slightly differently reuses the existing
        client ("L&T ECC Division" -> "L&T ECC"). Returns the effective site
        name — the existing canonical spelling, or the newly added name.
        """
        site = site.strip()
        if not site:
            return ""
        changed = False
        with self._lock:
            clients = self.load().setdefault("clients", {})
            key = self._canonical_key(clients, client)
            if key == client and key not in clients:
                # No exact (case-insensitive) client match — reuse a
                # near-same client instead of creating a duplicate.
                near = find_near_name(list(clients.keys()), client)
                if near is not None:
                    key = str(near)
            sites = clients.setdefault(key, [])
            canonical = find_near_name(list(sites), site)
            if canonical is not None:
                if canonical != site:
                    print(f"[config] site '{site}' is the same site as '{canonical}' — reusing existing name")
                return canonical
            sites.append(site)
            self.save()
            changed = True
        if changed:
            self._mark_push_pending(reason=f"add site '{site}' to '{key}'")
        return site

    @staticmethod
    def _canonical_key(d: dict, name: str) -> str:
        """Return the existing dict key that case-insensitively matches name."""
        lowered = name.strip().lower()
        for k in d:
            if str(k).lower() == lowered:
                return k
        return name

    @staticmethod
    def _ci_matches(existing, name: str) -> bool:
        return any(str(e).lower() == name.strip().lower() for e in existing)

    def add_material(self, name: str, shortcode: str) -> None:
        changed = False
        with self._lock:
            materials = self.load().setdefault("materials", {})
            if materials.get(name) != shortcode:
                materials[name] = shortcode
                self.save()
                changed = True
        if changed:
            self._mark_push_pending(reason=f"add material '{name}'")

    def add_doc_type(self, doc_type: str) -> None:
        changed = False
        with self._lock:
            doc_types = self.load().setdefault("doc_types", [])
            if not self._ci_matches(doc_types, doc_type):
                doc_types.append(doc_type)
                self.save()
                changed = True
        if changed:
            self._mark_push_pending(reason=f"add doc type '{doc_type}'")
