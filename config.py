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
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from localsettings import (
    LocalSettings,
    app_directory,
    install_is_shared,
    is_network_path,
)
from ocr import (
    LEGACY_OCR_MODELS,
    OCR_API_BASE,
    OCR_MODEL,
    OCR_THINKING,
)

# Keys that describe the PERSON or the MACHINE rather than the shared catalog.
# On a SHARED install (everybody runs the exe from the same server folder) the
# shared config.json can only hold one value for each of these, so a person's
# own value lives in their per-user settings file and overrides the shared one
# (see localsettings.py). On a normal single-machine install nothing changes:
# the value is read from and written to config.json exactly as before.
MACHINE_KEYS = (
    "watch_directory",
    "root_directory",
    "auto_start",
    "enable_ocr",
    "ocr_model",
    "ocr_api_base",
    "ocr_thinking",
    "popup_delay_seconds",
    "enable_live_config",
    "enable_github_push",
)

# Catalog keys — the ones a shared config.json must never lose, and the ones a
# union merge (instead of a blind overwrite) protects when two people edit the
# shared file at the same time.
CATALOG_KEYS = (
    "companies",
    "company_initials",
    "clients",
    "materials",
    "doc_types",
    "client_aliases",
    "site_aliases",
    "material_aliases",
    "removed_clients",
)

# Remote live config — single source of truth for clients/sites.
# Every popup fetches this so all users see the same data instantly.
# NOTE: fetches go through the GitHub Contents API (see fetch_github_config)
# because raw.githubusercontent.com is CDN-cached for up to 5 minutes — a
# pull right after a push would read the stale pre-push file. The raw URL is
# kept only as a fallback when the API is unreachable.
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
    # Live GitHub config sync — when true the app fetches config.json from
    # GitHub (Contents API, never cached) ONCE when a popup opens, so a push
    # to config.json on GitHub appears for all users without rebuilding the
    # exe. There is deliberately NO background polling: a periodic pull would
    # fight hand edits and the tray force-push. Set to false to use only the
    # local config.json.
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
    # catalog. LOCAL-ONLY toggle: it is never synced from the GitHub config nor
    # pushed back, because OCR needs this machine's own API key (see
    # _read_opencode_token) and one machine enabling it must not force it on
    # every install.
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
    # Name MAPPING (the 🗺 Map buttons next to Client/Site): when a name is
    # mapped — {source: target} — any popup that reads the SOURCE name
    # (from OCR, or typed/saved manually) switches it to the TARGET name
    # instead. This is how a recurring OCR spelling is pinned to the
    # catalog name it belongs to without renaming anything. client_aliases
    # maps clients to clients; site_aliases maps sites to sites;
    # material_aliases maps a word shown in the "Description of Goods"
    # column to a MATERIAL (e.g. "nuts"/"bolts" -> "Screw", "hinges" ->
    # "Fastner") so the popup pre-selects the right material even when the
    # document never writes the catalog name. All three are shared config
    # (synced/pushed like clients & materials).
    "client_aliases": {},
    "site_aliases": {},
    "material_aliases": {},
    # Clients removed by the cross-client site MOVE (see move_client_sites):
    # the GitHub union-merge re-adds any client that still exists remotely, so
    # a moved-away client needs an explicit tombstone to stay gone on every
    # machine. Cleared automatically when the client is explicitly re-added.
    "removed_clients": [],
    # Model + endpoint used by the OCR feature (OpenCode Go catalog,
    # OpenAI-compatible API). Overridable per machine in config.json; a value
    # that is merely an OLD DEFAULT (see ocr.LEGACY_OCR_MODELS) is upgraded to
    # the current model on load, so switching the default reaches installs
    # whose config.json pinned the previous name.
    "ocr_model": OCR_MODEL,
    "ocr_api_base": OCR_API_BASE,
    # How hard the OCR model may think before answering: "off" (default, by
    # far the fastest), "low"/"high"/"max" for graded thinking, or "default"
    # to send no thinking field at all and take the model's own default
    # (which is "high" — the slow one). Thinking mode is ON by default on the
    # DeepSeek API, and that chain of thought — not the image or the prompt —
    # is what made a single read take 30s+; OCR only copies fields off a
    # document, so it does not need to reason. The app walks a ladder
    # (reasoning_effort "none" -> thinking disabled -> graded effort -> model
    # default) and remembers which rung the gateway honours, so an endpoint
    # that refuses a field costs one extra round trip, never a broken read.
    # LOCAL-ONLY, like the model and the endpoint.
    "ocr_thinking": OCR_THINKING,
    # How long the watcher waits after a new file stops growing before the
    # popup opens (seconds). The file must also be unlocked, so a download
    # that is still running never pops up early; this only decides how long a
    # *finished* file sits in the watch folder before the popup appears. 1.0
    # is the default; raise it on a machine whose scanner/copier keeps a file
    # open (or writes in slow bursts), lower it for the snappiest popups.
    # LOCAL-ONLY, like the OCR keys.
    "popup_delay_seconds": 1.0,
    # --- Shared install (several people, one server folder) ----------------
    # "shared_install": true makes this folder a shared install: everybody runs
    # the same exe from it, config.json is the shared catalog (no GitHub round
    # trip), each person's own watch/root folder comes from their per-user
    # settings file instead (see localsettings.py), and the app does not update
    # itself in place — the admin updates the server copy.
    #
    # The key is deliberately NOT part of DEFAULT_CONFIG: a fresh config.json
    # must never carry an explicit "false" that would override the automatic
    # detection when the folder is later moved to a server share. Running the
    # exe from a network path is detected by itself; the flag is only needed
    # for a share Windows does not report as remote.
    #
    # Optional: pre-assign each person's watch folder here so nobody has to
    # choose one. Keys may be the Windows account ("manish"), account@pc
    # ("manish@pc-02") or "pc-02\\manish"; anyone not listed is asked once, on
    # their own machine, and their answer is remembered locally.
    "watch_directories": {},
}


def default_config_path() -> Path:
    """Return the path to the config.json file next to the app.

    When frozen (Nuitka standalone) the modules live inside the app folder, but
    ``__file__`` can point at a temporary/embedded location; the config file
    must always be found next to the running executable so the user's data is
    read (and new files are created there). On a shared install that same file
    is the catalog everybody sees.
    """
    return app_directory() / "config.json"


def default_token_path() -> Path:
    """Path to the file that holds the GitHub PAT for pushing config.json.

    The token is deliberately NOT stored in config.json — otherwise it would
    be pushed to the public repo when the config is synced. Store it in
    `github_token.txt` next to the exe (or set env FILEPICKER_GITHUB_TOKEN).
    """
    return app_directory() / "github_token.txt"


def default_opencode_token_path() -> Path:
    """Path to the file that holds the OpenCode Go API key used by OCR.

    Mirrors ``github_token.txt``: `opencode_token.txt` next to the exe (or
    env FILEPICKER_OPENCODE_TOKEN / OPENCODE_API_KEY). The key is the same
    one the opencode CLI uses (see `opencode auth`), and it is deliberately
    NEVER stored in config.json — otherwise it would be pushed to the public
    repo when the config is synced.
    """
    return app_directory() / "opencode_token.txt"


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

# Connector words that never tell two names apart. "&" is already folded into a
# space by the normaliser, so without this "Larsen and Toubro" and
# "Larsen & Toubro" normalise to different token lists ("larsen and toubro" vs
# "larsen toubro") and never match — the "and"/"&" spelling difference is
# exactly what the near-match is for (the OCR prompt used to carry the whole
# client list just to paper over this; the matcher does it itself now).
# Dropped anywhere in the name, not just at the front.
_SITE_CONNECTORS = {"and"}


def normalize_site_name(name) -> str:
    """Fold a site name into its comparable form.

    Lowercases, turns every non-alphanumeric run (punctuation, spacing,
    brackets, "&") into a single space, drops a leading article (a/an/the) and
    the connector "and" — so "The LODHA Shital-Baug", "Lodha shital baug" and
    "Larsen and Toubro"/"Larsen & Toubro" compare equal, while "Site A" and
    "Kalpataru Vivant (T-A)" keep their letters.
    """
    text = re.sub(r"[^0-9a-z]+", " ", str(name).lower())
    tokens = [t for t in text.split() if t and t not in _SITE_CONNECTORS]
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


def _token_cost(a: str, b: str) -> Optional[Tuple[int, int]]:
    """How far apart two (already normalised) words are — None when they are
    not the same word at all.

    ``(tier, letter_edits)``:
    * ``(0, 0)`` — the identical word;
    * ``(1, 0)`` — the same word but for its numbers ("t1" vs "t2", "site1"
      vs "site"): vendors and OCR write the numeral inconsistently, so it
      still counts as the same word, just not as close as an identical one —
      that is what keeps a catalog holding both "Client 1" and "Client 2"
      from answering "Client 2" with "Client 1";
    * ``(2, 1)`` — one letter off ("shital" vs "sital").
    Mirrors :func:`_site_tokens_near` exactly, but returns the distance so the
    CLOSEST catalog name can win instead of the first one listed.
    """
    if a == b:
        return (0, 0)
    if a.isdigit() and b.isdigit():
        return (1, 0)
    a_letters = re.sub(r"\d", "", a)
    b_letters = re.sub(r"\d", "", b)
    if a_letters and b_letters:
        if a_letters == b_letters:
            return (1, 0)
        if len(a_letters) >= 2 and len(b_letters) >= 2 \
                and _levenshtein(a_letters, b_letters) <= 1:
            return (2, 1)
    if not a or not b or len(a) < 2 or len(b) < 2:
        return None
    return (2, 1) if _levenshtein(a, b) <= 1 else None


def _token_match_cost(seq: List[str], sub: List[str]) -> Optional[Tuple[int, int, int]]:
    """``(extra words, worst tier, letter edits)`` for the closest way to read
    *sub* as *seq*, or None when they are not near-same.

    Same rule as :func:`_token_subsequence_matches` (at most ONE extra word,
    every other token within one letter, in order), but it reports how loose
    the match is: "lodha wood" is (1, 1, 0) against "lodha wood kandivali" and
    (1, 2, 1) against "lodha woods club", so the first one is the same place
    and the second one only looks like it. The tier comes from
    :func:`_token_cost`, so a name that matches literally always beats one
    that only matches because numbers are ignored.
    """
    if len(sub) > len(seq):
        seq, sub = sub, seq
    extra = len(seq) - len(sub)
    if extra > 1:
        return None
    best: Optional[Tuple[int, int, int]] = None
    for skipped in (range(len(seq)) if extra else [-1]):
        tiers: List[int] = []
        edits = 0
        for i, token in enumerate(sub):
            j = i if skipped < 0 or i < skipped else i + 1
            cost = _token_cost(seq[j], token)
            if cost is None:
                break
            tiers.append(cost[0])
            edits += cost[1]
        else:
            candidate = (extra, max(tiers), edits)
            if best is None or candidate < best:
                best = candidate
    return best


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
    # A unit TYPE is a designator too: "Lodha Nibm-T6 Pent House" is "Lodha
    # Nibm" (the user's report). Both spellings, because the value may be
    # written "Pent House" or "Penthouse".
    "pent", "penthouse",
    # A FACILITY inside a project is a designator too: "Lodha Palava-Fire
    # Station" is "Lodha Palava" (the user's report — the app matched the
    # stray catalog entry "Lodha Palava -Fire Station" instead of reading the
    # site as "Lodha Palava"). Both words, because the peel works one word at
    # a time ("Station", then "Fire").
    "fire", "station",
}

# The facility words above, on their own. A catalog entry that carries one of
# these in its trailing designator is a STRAY an older build saved and is
# resolved to its clean name (see find_near_site); the older unit words
# (tower/wing/phase/pent house...) keep the behaviour they have always had,
# because catalog entries like "Kalpataru Vivant Tower" or "Lodha Woods Club
# House" are names the user has been filing under for years.
_FACILITY_WORDS = {"fire", "station"}


# A trailing "letter + number" TOKEN is a unit designator spelled without a
# separator: "T6" is Tower 6 exactly like "T-6" and "Tower 6" (the user's
# document read "Lodha Wood-T6" and the T6 had to go). A digit is required, so
# a single letter alone ("Site A", "Kalpataru Vivant (T-A)") is NOT a
# designator and still decides matches.
_DESIGNATOR_TOKEN_RE = re.compile(r"(?i)^[a-z]\d+[a-z]?$")


def _strip_trailing_designator(tokens: List[str]) -> List[str]:
    """Drop ONE trailing unit designator: a standalone number, a "unit word +
    number" pair ('Tower 2', 'Phase 3'), or the short spellings of the same
    thing — "T6" (one token), "T-6"/"T 6" (normalised to "t 6"), "T2A".

    "Kalpataru Elitus Tower 2" is the same place as "Kalpataru Elitus" — the
    trailing designator is something vendors and OCR write inconsistently,
    so it never decides a match. Single-letter designators are never dropped
    ('Site A', 'Kalpataru Vivant (T-A)') and the strip never reduces a name
    to nothing.
    """
    if len(tokens) < 2:
        return tokens
    out = list(tokens)
    if _DESIGNATOR_TOKEN_RE.match(out[-1]):
        out = out[:-1]  # "lodha wood t6" -> "lodha wood"
    elif out[-1].isdigit():
        out = out[:-1]  # a trailing number itself never decides a match
        # "t-6"/"t 6" normalise to the two tokens "t 6": the designator letter
        # goes with the number. A unit WORD ("tower 2") goes the same way, but
        # only when a real name precedes it ("Sital Baug 2" keeps "Baug").
        if len(out) >= 2 and (re.fullmatch(r"[a-z]", out[-1])
                              or out[-1] in _UNIT_WORDS):
            out = out[:-1]
    return out or tokens


# A trailing STANDALONE dashed designator: "Raymond Premium T-B" is the
# same place as "Raymond Premium" — "T-B" is Tower B, written like "Tower 2"
# which is already ignored. Same for ranges: "T-9/10" and "T-9-10" are
# towers 9 & 10. The shape is one letter/digit, a dash or slash, then the
# number(s) — possibly chained ("T-9/10") — at the very END. A separator is
# what marks it as a designator token: bare trailing letters ("Site A")
# still never decide a match, multi-letter prefixes ("Parc-V", "Phase-A")
# look like real names, and bracketed forms ("Kalpataru Vivant (T-A)")
# stay part of the name.
#
# The same designator is also written with the SEPARATOR FIRST and the unit
# letter attached to the number — "Lodha Wood-T6", "Lodha Wood - T6",
# "Mirabella-T5 & 7" — which is Tower 6/Tower 5 exactly like "T-6"/"Tower 6".
# That spelling used to slip through, so the T6 stayed in the site name (the
# user's report: "It didnt remove T6 from <site>-T6"). And with no separator
# at all, just a space: "Lodha Wood T6". A single letter followed by digits
# is required in both, so real names that merely end in a dashed word
# ("Parc-V"), bracketed designators and single letters ("Site A") are still
# untouched.
_DASH_DESIGNATOR_RE = re.compile(
    r"(?i)(?:"
    # 1) the unit letter FIRST: "T-6", "T-9/10", "Wing-2/3". The lookbehind
    #    keeps the match a COMPLETE token: in "Wing-2/3" the "2/3" tail must
    #    not match on its own (the unit-word rule below handles the whole
    #    "Wing-2/3" token instead). Whitespace around the separator is
    #    allowed ("Tower -C" is written like "Tower-C").
    r"(?<![-/])\b[a-z0-9]\s*[-/]\s*[0-9a-z]+(?:\s*[/-]\s*[0-9a-z]+)*"
    # 2) the separator FIRST: "-T6", "-T 6", "-T-6", "-T6/7", "-T5 & 7".
    r"|[-/]\s*[a-z]\s*(?:[-/]\s*)?\d+[a-z]?(?:\s*[/&-]\s*[0-9a-z]+)*"
    # 3) no separator, just a space: "Lodha Wood T6", "Lodha Wood T 6".
    r"|\s+[a-z]\s*\d+[a-z]?"
    r")$"
)

# The same shape but spelled with a unit WORD — with or without a separator,
# and with or without a suffix. The word is a designator (see _UNIT_WORDS),
# so "Phase-2A", "Tower-B", "Tower -C", "Wing-2/3" are dropped just like
# "Tower 2" — AND the plain, un-dashed forms: a bare trailing word ("Lodha
# Amara Tower" == "Lodha Amara"), a word + single letter ("Tower B" — T-B
# spelled out, "Wing C", "Block A"), a word + number/range ("Tower 2",
# "Tower 9/10", "Phase 2A"). The suffixes are deliberately constrained: a
# whitespace-joined suffix must be a number (optionally with a trailing
# letter: "2A") or a SINGLE letter — anything longer ("Tower View",
# "Lodha Garden") is a real name, not a designator.
_UNIT_WORD_DESIGNATOR_RE = re.compile(
    r"(?i)\b(?:" + "|".join(sorted(_UNIT_WORDS))
    + r")\b"
    # 1) separator + value(s), chained: "Tower-C", "Wing-2/3", "Phase -2A"
    r"(?:\s*[-/]\s*[0-9a-z]+(?:\s*[/-]\s*[0-9a-z]+)*"
    # 2) whitespace + number, optionally with a trailing letter and ranges:
    #    "Tower 2", "Phase 2A", "Tower 9/10"
    r"|\s+[0-9]+[a-z]?(?:\s*[/-]\s*[0-9a-z]+)*"
    # 3) whitespace + a single letter: "Tower B", "Wing C", "Block A"
    r"|\s+[a-z])?$"
)


def _is_bare_unit_word(match_text: str) -> bool:
    """True when the unit-word match is just the word itself ("Tower"),
    i.e. no number/letter/range suffix follows it."""
    return match_text.strip().lower() in _UNIT_WORDS


def _word_count(text: str) -> int:
    """How many words a name fragment holds.

    Punctuation separates words for this purpose: "Antriksh-T6" is the place
    "Antriksh" plus a tower, i.e. TWO words — counting whitespace alone made
    it look like one, and the bare-unit-word guard below then refused to strip
    the "Pent" of "Antriksh-T6 Pent House" (the chained designator stopped
    after one step and the site kept "T6 Pent").
    """
    return len(re.findall(r"[0-9a-z]+", str(text).lower()))


def _one_edit_apart(a: str, b: str) -> bool:
    """True when *a* and *b* differ by at most one insertion/deletion/typo."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1:]


# Unit words are often MISSPELLED by OCR ("stattion" for "station") or written
# in the plural ("Towers"). A trailing word that is one edit away from a unit
# word of at least 5 letters is still a designator — the peel below uses this
# only when no exact designator matched, and only for the LAST word of a name
# that still keeps a real place name after the peel. Short unit words (fire,
# pod, flat, shop, zone, unit, wing, pent) are excluded: at 3-4 letters a
# one-edit neighbour is too easy to hit by accident.
_UNIT_WORD_MIN_FUZZY = 5


def _close_unit_word(token: str) -> bool:
    """True when *token* looks like a misspelled/plural unit word."""
    word = str(token).strip().lower()
    if len(word) < _UNIT_WORD_MIN_FUZZY or word in _UNIT_WORDS:
        return False
    return any(len(unit) >= _UNIT_WORD_MIN_FUZZY and _one_edit_apart(word, unit)
               for unit in _UNIT_WORDS)


# A trailing BRACKETED designator: "L & T (T-10)" is "L & T" — a tower/block/
# wing inside brackets is the same designator as the bare spelling (the user:
# "'L & T (T-10)' should also be changed to L & T"). Only a bracket whose
# content is NOTHING BUT a designator is dropped, so real qualifiers survive:
# "Acme Ozobe(Bellavista)", "L & T (Retail)" and "L & T Powai" are untouched.
_BRACKETED_TAIL_RE = re.compile(r"\(\s*([^()]*?)\s*\)\s*$")


# How many designator pieces may be peeled off one name. "Lodha Nibm-T6 Pent
# House" needs three ("House", "Pent", "T6"); the cap keeps a pathological
# value from being chipped away to nothing.
_DESIGNATOR_PEEL_LIMIT = 4

# What a peel trims off the name it keeps: whitespace, an opening bracket and
# the separator the designator was attached with — "Lodha Palava -Fire" must
# become "Lodha Palava", not "Lodha Palava -".
_PEEL_TRIM = " \t(/-"


def _peel_one_designator(raw: str, brackets: bool = True) -> str:
    """Drop ONE trailing designator (a bracketed one included).

    Returns *raw* unchanged when there is nothing to drop, so callers can loop
    until the value settles.
    """
    if brackets:
        m = _BRACKETED_TAIL_RE.search(raw)
        if m and is_designator_only(m.group(1)):
            peeled = raw[:m.start()].rstrip(_PEEL_TRIM)
            if peeled:
                return peeled
            # A bracket that was the whole name leaves nothing to keep; fall
            # through to the other rules, which will find nothing either.
    # Prefer the match that starts EARLIEST, i.e. the longest designator: in
    # "Kalpataru Elitus Tower 9/10" the unit-word rule sees "Tower 9/10" while
    # the dashed rule would only see the "9/10" tail, and stripping the tail
    # alone would leave a dangling "Tower" in the site name.
    matches = []
    for rx in (_DASH_DESIGNATOR_RE, _UNIT_WORD_DESIGNATOR_RE):
        m = rx.search(raw)
        if m:
            matches.append(m)
    for m in sorted(matches, key=lambda mm: mm.start()):
        out = raw[:m.start()].rstrip(_PEEL_TRIM)
        if not out:
            continue  # nothing but a designator — no place name to keep
        if _is_bare_unit_word(m.group(0)) and _word_count(out) < 2:
            # "Lodha Tower" / "Lodha Woods Club House": the bare unit word may
            # BE part of the name; only strip when a real name precedes it.
            continue
        return out
    # Nothing matched exactly: a trailing unit word that OCR misspelled or
    # pluralised ("Lodha Palava-Fire Stattion", "Lodha Amara Towers") is still
    # a designator. Same guard as above — a real multi-word place name must
    # remain in front of it.
    tokens = raw.split()
    if len(tokens) >= 2 and _close_unit_word(tokens[-1]):
        out = " ".join(tokens[:-1]).rstrip(_PEEL_TRIM)
        if out and _word_count(out) >= 2:
            return out
    return raw


def _strip_trailing_dash_designator(name, brackets: bool = True) -> str:
    """Drop every trailing designator ("Raymond Premium T-B",
    "Raymond Premium T-9/10", "Raymond Premium Phase-2A" -> "Raymond
    Premium"; "Kalpataru Elitus Tower", "Kalpataru Elitus Tower B",
    "Kalpataru Elitus Tower 9/10", "Kalpataru Elitus Wing C" -> "Kalpataru
    Elitus"; "L & T (T-10)" -> "L & T"; "Lodha Nibm-T6 Pent House" -> "Lodha
    Nibm"). Returns the input when nothing is stripped; never returns an empty
    string. A BARE trailing unit word is only treated as a designator when a
    multi-word place name precedes it ("Lodha Woods Club House" keeps "House"
    — it may BE the name; "Lodha Amara Tower" strips it) and a standalone
    designator ("Tower B") stays as-is here (callers use
    :func:`is_designator_only` to detect those).

    The designator is often CHAINED — a tower plus a unit type ("Nibm-T6 Pent
    House"), a phase plus a wing — so pieces are peeled one after another
    (at most :data:`_DESIGNATOR_PEEL_LIMIT`), each step using the same rules.

    *brackets* controls whether a designator written inside brackets counts
    ("L & T (T-10)"). It is on for the name a site is SHOWN and STORED as, and
    off for MATCHING (:func:`_match_forms`): the catalog legitimately holds
    "Raheja Solaris (Tower-A)" and "Raheja Solaris (Tower-B)" as two different
    sites, and a matcher that ignored the bracket would consider a Tower-B
    document the same place as Tower-A and file it in the wrong folder.
    """
    raw = str(name).strip()
    if not raw:
        return raw
    for _ in range(_DESIGNATOR_PEEL_LIMIT):
        peeled = _peel_one_designator(raw, brackets)
        if peeled == raw:
            break
        raw = peeled
    return raw


def is_designator_only(name) -> bool:
    """True when *name* is NOTHING but a unit designator.

    "Tower-A", "Tower -C", "T-9/10", "Phase-2", "Tower", "Tower B",
    "Wing C", "Tower 9/10", "T6" carry no place name at all — there is no
    site to save, so OCR values like these must not become a new site (the
    user picks the real one instead).
    """
    raw = str(name).strip()
    if not raw:
        return False
    if normalize_site_name(raw) in _UNIT_WORDS:
        # A lone unit word ("Tower", "Wing", "Block") is designator-only.
        return True
    if _DESIGNATOR_TOKEN_RE.match(normalize_site_name(raw).replace(" ", "")):
        # "T6" / "T 6" / "T-6" — tower 6 and nothing else.
        return True
    words = normalize_site_name(raw).split()
    if words and all(w in _UNIT_WORDS or _DESIGNATOR_TOKEN_RE.match(w)
                     for w in words):
        # "Pent House", "T6 Pent House", "Tower 2" — every word is a unit
        # word or a unit designator, so there is no place name in it.
        return True
    for rx in (_DASH_DESIGNATOR_RE, _UNIT_WORD_DESIGNATOR_RE):
        m = rx.search(raw)
        if m and not raw[:m.start()].strip(_PEEL_TRIM):
            return True
    return False


def _match_forms(name: str) -> tuple:
    """The comparable forms of a name (normal, designator-stripped).

    A name may compare equal through any of its forms: the plain normalized
    form, the numeric/unit-designator-stripped form ("Kalpataru Elitus
    Tower 2" -> "kalpataru elitus", "Lodha Wood T6" -> "lodha wood"), and
    the designator-stripped form — dashed ("Raymond Premium T-B",
    "Lodha Wood-T6" -> "raymond premium"/"lodha wood") or spelled out with a
    unit word ("Kalpataru Elitus Tower B" / "Kalpataru Elitus Wing C" ->
    "kalpataru elitus"). A designator inside BRACKETS is deliberately NOT
    stripped here: "Raheja Solaris (Tower-A)" and "Raheja Solaris (Tower-B)"
    are two different sites, and matching them as one would file a Tower-B
    note under Tower-A (the popup tries the printed value first and only then
    the bracket-stripped one — see ``_resolve_site_readonly``). Returns a
    tuple of the distinct forms, never empty.
    """
    raw = str(name).strip()
    norm = normalize_site_name(raw)
    forms = [norm]
    stripped = " ".join(_strip_trailing_designator(norm.split()))
    if stripped != norm:
        forms.append(stripped)
    dashless = normalize_site_name(
        _strip_trailing_dash_designator(raw, brackets=False))
    if dashless and dashless != norm and dashless not in forms:
        forms.append(dashless)
    return tuple(forms)


def find_near_name(existing_names, candidate) -> Optional[str]:
    """The existing catalog name that is the *same place* as ``candidate``.

    Applies to site and client names alike. Matching ignores case,
    punctuation, spacing, articles (a/an/the) and numbers — "T1"/"T2"/"Tower
    1"/"Tower 2" are the same site, so the exact numeral never blocks a
    match. A trailing unit designator is ignored too, in every spelling:
    "Kalpataru Elitus Tower 2" matches "Kalpataru Elitus", "Kalpataru
    Elitus Tower B" matches "Kalpataru Elitus" (T-B spelled out = Tower B),
    "Kalpataru Elitus Wing C" / "Block A" / "Tower 9/10" as well (and
    "Lodha Shital Baug Tower 2" matches "Sital Baug" — the brand prefix AND
    the designator are both tolerated; "Raymond Premium T-B" matches
    "Raymond Premium"). The same designator written the other way round is
    ignored too: "Lodha Wood-T6" / "Lodha Wood T6" / "Lodha Wood T-6" are
    all "Lodha Wood". Tolerates one-letter spelling variants per word
    ("shital bag" vs "Sital Baug", "Larsen and Toubro" vs "Larsen &
    Toubro") and at most one extra word (brand prefixes like "Lodha").
    Names that differ only in spacing/punctuation ("T-A" vs "TA" vs "T A")
    are equivalent. Deliberately strict: names that merely share words are
    NOT matched ("Sai Baug" is never "Sital Baug"), single-letter tokens are
    exact-only ("Site A" is never "Site B"), and a BARE trailing unit word
    is only ignored when a real name precedes it ("Lodha Amara Tower" is
    "Lodha Amara", but "Lodha Woods Club House" keeps "House" — it may BE
    the name).

    When SEVERAL catalog names are near-same, the CLOSEST one wins — fewest
    extra words first, then an identical spelling over one that only matches
    because numbers are ignored, then fewest one-letter differences — and
    catalog order only breaks an exact tie. "Lodha Wood" is near both "Lodha
    Woods Club House" and "LODHA - WOOD-kandivali"; only the second one is the
    same words with no spelling change, so that is the site the document means
    (picking the first one listed is how a wrong site ends up in the folder
    path). The same rule keeps a catalog that holds both "Client 1" and
    "Client 2" from answering "Client 2" with "Client 1". Returns the
    canonical existing spelling.
    """
    cand_forms = _match_forms(candidate)
    if not cand_forms or not cand_forms[0]:
        return None

    best_name: Optional[str] = None
    best_cost: Optional[Tuple[int, int, int, int]] = None
    for name in existing_names:
        name_forms = _match_forms(name)
        if not name_forms or not name_forms[0]:
            continue
        # Either form pair may match (original, or one of the stripped
        # forms) — same words with only spacing/punctuation differences,
        # or near-identical word lists. The MATCHING rule is unchanged
        # (_token_subsequence_matches); the cost only ranks the matches.
        # The last element says how many of the two forms had to be
        # designator-stripped, so an identical name always beats one that
        # only matches once a trailing number/designator is dropped
        # ("Client 2" must not answer with "Client 1").
        cost: Optional[Tuple[int, int, int, int]] = None
        for a_index, a in enumerate(cand_forms):
            for b_index, b in enumerate(name_forms):
                rank = (0 if a_index == 0 else 1) + (0 if b_index == 0 else 1)
                if a == b or a.replace(" ", "") == b.replace(" ", ""):
                    this: Tuple[int, int, int, int] = (0, 0, 0, rank)
                elif _token_subsequence_matches(a.split(), b.split()):
                    extra, tier, edits = _token_match_cost(
                        a.split(), b.split()) or (9, 9, 9)
                    this = (extra, tier, edits, rank)
                else:
                    continue
                if cost is None or this < cost:
                    cost = this
                if cost == (0, 0, 0, 0):
                    break
            if cost == (0, 0, 0, 0):
                break
        if cost is None:
            continue
        if best_cost is None or cost < best_cost:
            best_name, best_cost = str(name), cost
            if best_cost == (0, 0, 0, 0):
                break  # an identical name can never be beaten
    return best_name


def find_near_site(existing_sites, candidate, prefer_clean: bool = True) -> Optional[str]:
    """The existing site that is the same place as *candidate*, else None.

    Same rules as :func:`find_near_name` (see there), plus ONE rule that only
    makes sense for sites: when the catalog entry that matches is itself
    spelled with a trailing unit designator and the candidate reduces to that
    entry's CLEAN name, the clean spelling is returned instead of the stored
    one.

    The catalog still holds strays an older build saved with a designator
    ("Lodha Palava -Fire Station", "Lodha Nibm -T6 Pent"). A designator is not
    part of a site name (the same rule that keeps it out of new sites — see
    :meth:`ConfigManager.add_site`), so a document that says "Lodha Palava
    -Fire Station" must not be answered with the stray spelling: the site is
    "Lodha Palava" (the user's report: "It read lodha palava-fire stattion as
    the same when it should have been lodha palava only"). The stray entry is
    left untouched — callers that store a site add the clean spelling as its
    own catalog entry, and an identical catalog entry always wins over a
    stray anyway.

    Pass ``prefer_clean=False`` for the plain near-match contract (the result
    is then always one of ``existing_sites``).
    """
    hit = find_near_name(existing_sites, candidate)
    if hit is None or not prefer_clean:
        return hit
    clean_hit = clean_site_name(hit)
    if clean_hit == str(hit).strip():
        return hit  # the catalog spelling is already the clean one
    # Only a FACILITY tail ("-Fire Station") makes the stored spelling a
    # stray: the older unit words are left exactly as they were, so a site
    # filed under "Kalpataru Vivant Tower" or "Lodha Woods Club House" keeps
    # its name (and its folder).
    if not _facility_tail(str(hit), clean_hit):
        return hit
    # The stored name carries a facility. Substitute its clean form only when
    # the candidate is the same place — i.e. it reduces to that very clean
    # name, typos/designators in its tail included.
    if _peel_to_name(candidate, clean_hit):
        return clean_hit
    return hit


def _facility_tail(stored, clean) -> bool:
    """True when the words *stored* adds to *clean* include a facility word.

    "Lodha Palava -Fire Station" vs "Lodha Palava" -> the tail is
    ("fire", "station") -> True. "Kalpataru Vivant Tower" vs "Kalpataru
    Vivant" -> ("tower") -> False.
    """
    stored_words = normalize_site_name(stored).split()
    clean_words = normalize_site_name(clean).split()
    if len(stored_words) <= len(clean_words):
        return False
    return any(word in _FACILITY_WORDS
               for word in stored_words[len(clean_words):])


def clean_site_name(name) -> str:
    """*name* without a trailing unit designator written OUTSIDE brackets.

    "Lodha Palava -Fire Station" -> "Lodha Palava", "Lodha Wood-T6" ->
    "Lodha Wood". Bracketed designators are kept ("Raheja Solaris (Tower-A)"
    is a different site from "(Tower-B)" — see :func:`_strip_trailing_dash_designator`).
    """
    return _strip_trailing_dash_designator(name, brackets=False)


def _peel_to_name(candidate, clean_name) -> bool:
    """True when *candidate* is *clean_name* plus a trailing designator.

    "lodha palava-fire stattion" -> "Lodha Palava": the words of *clean_name*
    must open the candidate, and everything after them must be designator-ish
    — a number, a "T6"-style token, a unit word, or a near-miss of one
    ("stattion"). Case/spacing/punctuation and a leading article are ignored,
    exactly like :func:`normalize_site_name`.
    """
    words = normalize_site_name(clean_name).split()
    cand = normalize_site_name(candidate).split()
    if not words or len(cand) < len(words):
        return False
    for want, got in zip(words, cand):
        if want != got and not _one_edit_apart(want, got):
            return False
    for token in cand[len(words):]:
        if (token.isdigit() or _DESIGNATOR_TOKEN_RE.match(token)
                or token in _UNIT_WORDS or _close_unit_word(token)):
            continue
        return False
    return True


# How long a copy waits for another copy's write into the shared config.json,
# and when a lock file left behind by a crash is considered stale.
_SHARED_LOCK_TIMEOUT = 5.0
_SHARED_LOCK_STALE = 30.0


@contextmanager
def _shared_write_lock(path: Path, timeout: float = _SHARED_LOCK_TIMEOUT):
    """Serialise writes into a shared config.json (best effort).

    Two copies running from the same server folder can save at the same moment.
    A lock file next to config.json makes them take turns, so the read-merge-
    write in :meth:`ConfigManager.save` always sees the colleague's completed
    write instead of a half-applied one. Creating the file with ``O_EXCL`` is
    atomic (SMB included). A lock older than 30s is treated as stale — a crash
    left it behind — and broken. If the lock still cannot be taken in time the
    save goes ahead: a slightly racy write beats refusing the user's edit.
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    fd: Optional[int] = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _SHARED_LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                print("[config] shared config.json is locked by another copy "
                      "— saving without the lock")
                break
            time.sleep(0.05)
        except OSError:
            # No permission to create the lock (read-only folder, odd share):
            # let the write itself report the real error.
            break
    try:
        yield fd is not None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


class ConfigManager:
    """Thread-safe wrapper around the persistent config.json file.

    Reads the file lazily, caches the parsed structure in memory, and writes
    every mutation back to disk so the config is always up to date. On a shared
    install (see :attr:`shared_install`) the same file is read and written by
    every person's copy, so writes are locked and union-merged.
    """

    def __init__(self, path: Optional[Path] = None,
                 local: Optional[LocalSettings] = None,
                 shared: Optional[bool] = None) -> None:
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
        # Per-person settings (watch folder, OCR keys, ...) that override this
        # shared file on a server install — see localsettings.py.
        self._local = local if local is not None else LocalSettings()
        # Shared-install state. ``shared`` forces it (tests / explicit callers);
        # otherwise it is decided once from FILEPICKER_SHARED, config.json's
        # "shared_install" flag and the app folder being a network path.
        self._network_shared = is_network_path(self.path.parent)
        self._shared: Optional[bool] = shared
        self._shared_forced = shared is not None
        # The catalog exactly as this copy last read or wrote it: what tells a
        # shared install whether the file on disk moved on without us (a
        # colleague's save), which is what triggers the union merge.
        self._saved_catalog: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Shared install
    # ------------------------------------------------------------------
    @property
    def shared_install(self) -> bool:
        """True when everybody runs this copy from the same server folder.

        Then config.json is the shared catalog (there is no need for the GitHub
        round trip), the machine-specific keys come from this person's own
        settings file, writes into the shared file are union-merged instead of
        overwriting a colleague's simultaneous edit, and the app does not
        replace its own exe in a folder other people are running from.
        """
        if self._shared is None:
            flag = None
            if self._loaded:
                raw = self._data.get("shared_install")
                if isinstance(raw, bool):
                    flag = raw
            self._shared = install_is_shared(
                self.path.parent, flag,
                config_path=self.path if flag is None else None,
            )
        return self._shared

    def local_override(self, key: str) -> Any:
        """This person's own value for a machine key, else None.

        ``None`` on a normal install (there is nothing to override), which is
        what keeps the single-machine behaviour byte-for-byte identical.
        """
        if not self.shared_install:
            return None
        return self._local.get(key)

    def local_setting(self, key: str, default: Any = None) -> Any:
        """Any per-person setting, whatever the install mode.

        Used for bookkeeping the app keeps about this person rather than a
        config value — e.g. "they declined the watch-folder question, so do not
        ask again on this machine".
        """
        return self._local.get(key, default)

    def _machine_value(self, key: str, default: Any = None) -> Any:
        """A machine key: this person's override first, else the shared file."""
        override = self.local_override(key)
        if override is not None:
            return override
        return self.load().get(key, default)

    def _set_machine_value(self, key: str, value: Any) -> None:
        """Write a machine key where it belongs: per person, or config.json."""
        with self._lock:
            if self.shared_install:
                self._local.set(key, value)
                return
            self.load()[key] = value
            self.save()

    def _catalog_snapshot(self) -> Dict[str, Any]:
        """The catalog keys as this copy last read or wrote them."""
        return deepcopy({key: self._data.get(key) for key in CATALOG_KEYS})

    def refresh_from_shared_file(self) -> bool:
        """Re-read the shared config.json when a colleague changed it.

        On a server install the file IS the live config (no GitHub poll), so a
        site/client a colleague added a moment ago must reach this machine's
        next popup. Local, un-pushed catalog additions are union-merged in, so a
        reload can never drop something this machine just added.

        Returns True when the in-memory catalog changed.
        """
        if not self.shared_install or not self._loaded:
            return False
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    disk = json.load(fh)
            except (OSError, json.JSONDecodeError, ValueError):
                return False
            if not isinstance(disk, dict):
                return False
            if {k: disk.get(k) for k in CATALOG_KEYS} == self._saved_catalog:
                return False  # nothing new since this copy last looked
            before = self._catalog_snapshot()
            merged = self._merge_for_push(disk, self._data)
            for key in CATALOG_KEYS:
                if key in merged:
                    self._data[key] = merged[key]
            for key, value in disk.items():
                if key not in CATALOG_KEYS and key not in MACHINE_KEYS:
                    self._data.setdefault(key, value)
            self._saved_catalog = self._catalog_snapshot()
            self._last_write_mtime = self._file_mtime(self.path)
            self._write_mtime_marker(self._last_write_mtime)
            changed = before != self._saved_catalog
            if changed:
                print("[config] shared config.json changed by another machine "
                      "— catalog reloaded")
            return changed

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
            self._saved_catalog = self._catalog_snapshot()
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
            # Upgrade values that an older build wrote as its then-current
            # default (currently only the OCR model) — see the method.
            self._migrate_old_defaults()
            # The file itself can declare the install shared; re-decide now
            # that its flag is known (never when the caller forced the mode).
            if not self._shared_forced:
                self._shared = None
            self._saved_catalog = self._catalog_snapshot()
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

    def _migrate_old_defaults(self) -> None:
        """Upgrade config values that are merely an OLD DEFAULT of this app.

        ``ocr_model`` is a local-only key, so config.json is the source of
        truth for it: an install that ran an older build has the old model id
        written in the file, and changing the default in ``ocr.py`` would
        never reach it. A stored value from :data:`ocr.LEGACY_OCR_MODELS` is
        therefore treated as "never chosen by the user" and replaced with the
        current :data:`ocr.OCR_MODEL` — in memory here, and on disk at the
        next :meth:`save` (no surprise write during load, so a hand-edited
        config keeps its mtime and is not mistaken for an external edit).
        Any other value is a deliberate per-machine override: left alone.
        """
        model = self._data.get("ocr_model")
        if isinstance(model, str) and model.strip() in LEGACY_OCR_MODELS:
            print(f"[config] OCR model '{model}' is an old default — "
                  f"upgrading to '{OCR_MODEL}'")
            self._data["ocr_model"] = OCR_MODEL

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
        """Fetch the live config from GitHub. Returns None on failure.

        Uses the GitHub Contents API — the same endpoint the GitHub web UI
        reads — which is NEVER cached, so a fetch moments after a push
        always sees the current file (raw.githubusercontent.com is CDN-cached
        for up to ~5 minutes and can serve a stale pre-push config, which
        made force-pull "revert" to an older config). Authenticates with the
        push token when available (avoids API rate limits). Falls back to the
        cache-busted raw URL only when the API is unreachable. Every
        successful fetch logs the sha of the version read.
        """
        def _get_json(url: str, t: float) -> Any:
            import urllib.request
            headers = {
                "User-Agent": "FilePicker",
                "Accept": "application/vnd.github+json",
            }
            token = _read_github_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=t) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            # 1) GitHub Contents API — always the current file.
            try:
                info = _get_json(f"{GITHUB_API_URL}?ref={GITHUB_BRANCH}", timeout)
                content_b64 = info.get("content") or ""
                if info.get("encoding") == "base64" and content_b64:
                    data = json.loads(base64.b64decode(content_b64).decode("utf-8"))
                    if isinstance(data, dict) and "clients" in data:
                        print(f"[config] live config fetched via GitHub API "
                              f"(sha={str(info.get('sha'))[:7]})")
                        return data
            except Exception as exc:
                print(f"[config] GitHub API fetch failed "
                      f"({getattr(exc, 'code', None) or type(exc).__name__}); "
                      "falling back to raw URL")
            # 2) Raw fallback, cache-busted (best effort).
            import time as _time
            url = GITHUB_CONFIG_URL
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}_t={int(_time.time())}"
            data = _get_json(url, timeout)
            if isinstance(data, dict) and "clients" in data:
                return data
        except Exception as exc:
            print(f"[config] GitHub live config fetch failed: {exc}")
        return None

    @property
    def enable_live_config(self) -> bool:
        """Whether to poll GitHub for live config.

        On a SHARED install the config.json everybody runs from already IS the
        live config, so the GitHub round trip is off by default — a poll would
        only fight the shared file (and re-introduce the "somebody pushed a
        stale catalog" failure). A per-person override can still turn it on.
        """
        override = self.local_override("enable_live_config")
        if override is not None:
            return bool(override)
        if self.shared_install:
            return False
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
            for key in ("companies", "company_initials", "clients", "materials",
                        "doc_types", "client_aliases", "site_aliases", "material_aliases",
                        "removed_clients"):
                if key not in remote or remote[key] == self._data.get(key):
                    continue
                if key == "clients" and isinstance(remote[key], dict):
                    # A forced pull must not undo a site MOVE: clients the
                    # user moved away are tombstoned, so the still-old remote
                    # entry for them is skipped (unless the client was
                    # explicitly re-added locally).
                    removed = {str(r).strip().lower()
                               for r in (self._data.get("removed_clients") or [])
                               if str(r).strip()}
                    local_names = {str(k).strip().lower()
                                   for k in (self._data.get("clients") or {})}
                    pruned = {
                        k: v for k, v in remote[key].items()
                        if str(k).strip().lower() not in removed
                        or str(k).strip().lower() in local_names
                    }
                    self._data[key] = deepcopy(pruned)
                    changed = True
                    continue
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
            for key in ("companies", "company_initials", "clients", "materials",
                        "doc_types", "client_aliases", "site_aliases", "material_aliases",
                        "removed_clients"):
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

        On a SHARED install the shared config.json is the catalog everybody
        reads, so pushing to GitHub is pointless (and was what once replaced
        the real catalog with a test one) — off unless a per-person override
        turns it back on.
        """
        override = self.local_override("enable_github_push")
        if override is not None:
            return bool(override)
        if self.shared_install:
            return False
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
            catalog_keys = ("companies", "company_initials", "clients", "materials",
                            "doc_types", "client_aliases", "site_aliases", "material_aliases",
                            "removed_clients")
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

        # Clients a MOVE deleted (see move_client_sites) are tombstoned: the
        # union below would otherwise resurrect them from the stale remote
        # copy (and the next auto-pull would bring them back locally), so a
        # tombstoned client is dropped from BOTH sides here. The tombstones
        # themselves are union-merged further down, so every machine honours
        # the removal.
        def _tombstones(*sources) -> set:
            out = set()
            for src in sources:
                vals = src.get("removed_clients", []) if isinstance(src, dict) else []
                if not isinstance(vals, list):
                    continue
                for v in vals:
                    name = str(v).strip().lower()
                    if name:
                        out.add(name)
            return out

        removed_names = _tombstones(remote, local)
        # A name that the LOCAL config still has is not removed: an explicit
        # re-add (add_client/add_site, which also clears the local tombstone)
        # always wins over a stale tombstone that other machines still carry.
        local_names = {str(k).strip().lower() for k in loc_clients}

        for k, v in rem_clients.items():
            key = str(k)
            low = key.strip().lower()
            if low in removed_names and low not in local_names:
                continue  # moved away — never resurrect it from the remote
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

        # removed_clients — union of the tombstones (case-insensitive dedupe),
        # minus any name that exists in the merged catalog: a tombstone never
        # outlives an explicit re-add, and it is dropped from the published
        # list as soon as the client is back.
        rem_tomb = remote.get("removed_clients", [])
        loc_tomb = local.get("removed_clients", [])
        present = {str(k).strip().lower() for k in merged_clients}
        merged["removed_clients"] = [
            r for r in _merge_list_str(
                rem_tomb if isinstance(rem_tomb, list) else [],
                loc_tomb if isinstance(loc_tomb, list) else [])
            if str(r).strip().lower() not in present
        ]

        # materials — dict union, local wins
        rem_mat = dict(remote.get("materials", {}))
        loc_mat = dict(local.get("materials", {}))
        merged_mat = dict(rem_mat)
        merged_mat.update({str(k): str(v) for k, v in loc_mat.items()})
        merged["materials"] = merged_mat

        # client_aliases / site_aliases — dict union, local wins (same rule
        # as materials: a mapping added locally but not yet pushed is never
        # lost when the remote is still stale).
        for alias_key in ("client_aliases", "site_aliases",
                             "material_aliases"):
            rem_al = dict(remote.get(alias_key, {}))
            loc_al = dict(local.get(alias_key, {}))
            merged_al = dict(rem_al)
            merged_al.update({str(k): str(v) for k, v in loc_al.items()})
            merged[alias_key] = merged_al

        # doc_types — union list
        rem_docs = list(remote.get("doc_types", []))
        loc_docs = list(local.get("doc_types", []))
        merged["doc_types"] = _merge_list_str(rem_docs, loc_docs)

        return merged

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self) -> None:
        """Write the current in-memory config to disk atomically.

        On a SHARED install the file may have been written by a colleague since
        this copy last read it; the write is then union-merged (and serialised
        with a lock file) so two people adding a client/site at the same moment
        never lose each other's change.
        """
        with self._lock:
            # Persist the old-default upgrade (e.g. the OCR model) with the
            # next normal write instead of writing during load.
            self._migrate_old_defaults()
            if self.shared_install:
                with _shared_write_lock(self.path):
                    self._merge_external_changes()
                    self._write_file()
                return
            self._write_file()

    def _write_file(self) -> None:
        """The actual atomic write (unique temp name: several copies may write)."""
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._last_write_mtime = self._file_mtime(self.path)
            self._write_mtime_marker(self._last_write_mtime)
            self._saved_catalog = self._catalog_snapshot()
        except OSError as exc:
            print(f"[config] Could not write {self.path}: {exc}")

    def _merge_external_changes(self) -> None:
        """Union-merge the shared file when another copy wrote it first.

        Last-writer-wins is fine for one machine, but with everybody running the
        same server folder it silently drops the colleague's new client/site.
        The catalog keys are therefore union-merged (same rules as the GitHub
        push: nothing is lost, tombstones still delete) and every other key is
        taken from the file, which is the shared source of truth.

        The comparison is against the catalog THIS copy last read or wrote, not
        against a timestamp: on a server folder two saves can land inside the
        same second, and the 1s mtime epsilon used for hand-edit detection would
        miss exactly the case this exists for.
        """
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(disk, dict):
            return
        if {k: disk.get(k) for k in CATALOG_KEYS} == self._saved_catalog:
            return  # the file still holds what this copy last saw
        merged = self._merge_for_push(disk, self._data)
        new_data = dict(disk)
        for key in CATALOG_KEYS:
            if key in merged:
                new_data[key] = merged[key]
        for key, value in self._data.items():
            if key not in new_data:
                new_data[key] = value
        self._data = new_data
        print("[config] shared config.json was written by another copy — "
              "union-merged before saving")

    # ------------------------------------------------------------------
    # Typed accessors
    # ------------------------------------------------------------------
    @property
    def watch_directory(self) -> str:
        """Where THIS person's downloads land (per-user on a shared install)."""
        return str(self._machine_value("watch_directory", ""))

    @property
    def root_directory(self) -> str:
        """Where THIS person's sorted tree lives (per-user on a shared install)."""
        return str(self._machine_value("root_directory", ""))

    @property
    def watch_directories(self) -> Dict[str, str]:
        """Optional admin pre-assignment of watch folders, per person.

        ``{"manish": "Z:/Unsorted/Manish", "nitin@pc-02": "Z:/Unsorted/Nitin"}``
        — see :func:`localsettings.assigned_watch_folder` for the key forms.
        """
        mapping = self.load().get("watch_directories", {})
        if not isinstance(mapping, dict):
            return {}
        return {str(k): str(v) for k, v in mapping.items()
                if isinstance(v, str) and v.strip()}

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

    def find_similar_site_other_client(self, client: str, site: str):
        """Every OTHER client that already has *site* (near-match), if any.

        Returns a list of ``(other_client, their_site_spelling)`` pairs — one
        per other client whose site list contains the same place as *site*
        (same near-match rules as :func:`find_near_name`, so designators,
        spacing, punctuation, one-letter variants and one extra word are all
        ignored). The CURRENT client is skipped: a site that is already yours
        is normal, and a match there is resolved by the ordinary canonical
        lookup instead.

        This is the check behind the popup's cross-client warning. It only
        REPORTS — nothing is moved or reassigned here, ever (see
        :meth:`move_client_sites` for the explicit, user-requested move).
        """
        out = []
        candidate = str(site or "").strip()
        if not candidate:
            return out
        current = str(client or "").strip().lower()
        with self._lock:
            clients = self.load().get("clients", {})
            for name, sites in clients.items():
                if str(name).strip().lower() == current:
                    continue
                hit = find_near_name(list(sites or []), candidate)
                if hit is not None:
                    out.append((str(name), str(hit)))
        return out

    def move_client_sites(self, source_client: str, target_client: str) -> List[str]:
        """Move every site of *source_client* into *target_client*.

        The user-driven half of the cross-client warning ("move all sites from
        other client to present one which we save"): each of the source
        client's sites is appended to the target's list unless the target
        already has the same place (near-match dedupe keeps the target's
        existing spelling); the emptied source client is then removed from the
        catalog. Never called automatically — only after the user picks
        "move" in the warning dialog.

        The move is durable and applies to FUTURE documents too:

        - the source client is TOMBSTONED in ``removed_clients``, because the
          GitHub union-merge would otherwise resurrect it from the stale
          remote copy (and the next auto-pull would bring it back locally), so
          the move would silently undo itself;
        - a client MAPPING (``client_aliases``) is added from the source name
          to the target, so any later document/OCR that still names the old
          client resolves to the target automatically.

        Returns the target client's site list after the move. Pushes are
        deferred like every other mutator (the push happens once a file is
        actually saved, or on the tray force-push).
        """
        source = str(source_client or "").strip()
        target = str(target_client or "").strip()
        if not source or not target:
            return []
        changed = False
        with self._lock:
            data = self.load()
            clients = data.setdefault("clients", {})
            src_key = self._canonical_key(clients, source)
            tgt_key = self._canonical_key(clients, target)
            if src_key not in clients:
                # Nothing to move (already gone / unknown name) — leave the
                # rest of the config exactly as it is.
                return list(clients.get(tgt_key, []))
            if str(src_key).strip().lower() == str(tgt_key).strip().lower():
                return list(clients.get(src_key, []))
            target_sites = clients.setdefault(tgt_key, [])
            for site in list(clients.get(src_key, [])):
                if not str(site).strip():
                    continue
                if find_near_name(list(target_sites), site) is None:
                    target_sites.append(site)
                    changed = True
            del clients[src_key]
            changed = True
            # Tombstone: keeps the moved-away client gone through the union
            # merge on push AND on every later auto-pull.
            removed = data.get("removed_clients")
            if not isinstance(removed, list):
                removed = []
                data["removed_clients"] = removed
            if not any(str(r).strip().lower() == str(src_key).strip().lower()
                       for r in removed):
                removed.append(str(src_key))
            # Map the old client name to the new one for FUTURE cases: a
            # later document that still says "LODHA" is filed under the client
            # it was merged into.
            aliases = data.get("client_aliases")
            if not isinstance(aliases, dict):
                aliases = {}
                data["client_aliases"] = aliases
            alias_key = str(src_key)
            for k in list(aliases):
                if str(k).strip().lower() == str(src_key).strip().lower():
                    alias_key = str(k)
                    break
            if aliases.get(alias_key) != str(tgt_key):
                aliases[alias_key] = str(tgt_key)
            self.save()
            result = list(target_sites)
        if changed:
            self._mark_push_pending(
                reason=f"move all sites from '{src_key}' to '{tgt_key}'")
        return result

    def site_display_name(self, site: str) -> str:
        """*site* with a trailing unit designator removed.

        "Raymond Premium T-B" -> "Raymond Premium", "X T-9/10" -> "X",
        "Kalpataru Elitus Tower B" -> "Kalpataru Elitus". Used wherever a
        site is shown or stored (the popup's read-only resolution AND
        add_site) so a tower/phase/block/wing designator never lands in the
        field or in config.json. See
        :func:`_strip_trailing_dash_designator`.
        """
        return _strip_trailing_dash_designator(site)

    def site_is_designator_only(self, site: str) -> bool:
        """True when *site* is nothing but a unit designator ("Tower-A").

        Such a value carries no place name, so OCR must not put it in the
        site field (and it must never become a new site) — the user picks
        the real site instead. See :func:`is_designator_only`.
        """
        return is_designator_only(site)

    def find_near(self, existing_names, candidate) -> Optional[str]:
        """The catalog name that is the same place as ``candidate``.

        The generic near-match (used for CLIENT names as well as sites — the
        rules are identical: numbers ignored, one-letter variants and one
        extra word tolerated, case/spacing/punctuation/articles don't
        matter). See :func:`find_near_name`.
        """
        return find_near_name(existing_names, candidate)

    def all_clients(self) -> List[str]:
        """Every client name (used by the mapping dialog and the catalog view)."""
        return [str(k) for k in self.clients]

    def all_sites(self) -> List[str]:
        """Every site name across all clients (used by the 🗺 Map Site dialog)."""
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
        """Whether the app should register itself to launch at Windows login.

        Per machine on a shared install: the Windows entry is written into each
        person's own profile, so one person turning it on must not decide it for
        everybody (the shared config.json keeps the default).
        """
        value = self._machine_value("auto_start", True)
        return bool(value)

    @property
    def enable_ocr(self) -> bool:
        """Whether the popup auto-fills Company/Client/Site via OCR.

        Reads the LOCAL config only — like watch_directory/root_directory,
        this flag is never merged from the GitHub config nor pushed back, so
        one machine can enable OCR without forcing it on all installs. On a
        shared install it is per person (the API key and the model are, too).
        """
        return bool(self._machine_value("enable_ocr", False))

    @property
    def ocr_model(self) -> str:
        """The model id used for OCR (OpenCode Go catalog).

        A value that is merely an OLD DEFAULT of this app (see
        :data:`ocr.LEGACY_OCR_MODELS`) is upgraded to the current model, so an
        install whose config.json pinned the previous name switches over
        without the user editing anything. Any other value is a deliberate
        per-machine override and is returned as-is.
        """
        model = str(self._machine_value("ocr_model", OCR_MODEL)).strip()
        if not model or model in LEGACY_OCR_MODELS:
            return OCR_MODEL
        return model

    @property
    def ocr_api_base(self) -> str:
        """The OpenAI-compatible endpoint base used for OCR."""
        return str(self._machine_value("ocr_api_base", OCR_API_BASE))

    @property
    def ocr_thinking(self) -> str:
        """How hard the OCR model may think before answering (the fast knob).

        One of :data:`ocr.OCR_THINKING_LEVELS`: ``"off"`` (the default — no
        chain of thought at all, which is what makes a read take ~3s instead
        of 10-60s), ``"low"``/``"high"``/``"max"`` for graded thinking, or
        ``"default"`` to send nothing and accept the model's own default
        (``high``, the slow one). LOCAL-ONLY, like ``ocr_model``: never synced
        or pushed.

        The legacy ``ocr_reasoning_effort`` key is still honoured when it asks
        for graded thinking explicitly (``high``/``max``/``medium``); its old
        default ``"low"`` now resolves to the new default, because "low" was
        what 0.6.33 shipped rather than a user choice — and it did not make
        reads fast, since ``low`` still thinks (DeepSeek maps ``medium`` up to
        ``high`` and only ``none`` stops the chain of thought).
        """
        data = self.load()
        override = self.local_override("ocr_thinking")
        if override is not None:
            return str(override).strip().lower()
        value = data.get("ocr_thinking")
        if value is not None and str(value).strip():
            return str(value).strip().lower()
        legacy = str(data.get("ocr_reasoning_effort") or "").strip().lower()
        if legacy in ("high", "max", "medium", "xhigh"):
            return "high" if legacy in ("medium", "xhigh") else legacy
        return OCR_THINKING

    @property
    def popup_delay_seconds(self) -> float:
        """Seconds to wait after a new file stops growing before popping up.

        ``popup_delay_seconds`` in config.json (default 1.0, clamped to
        0.0-10.0). The watcher always waits for the file to be unlocked as
        well, so this is only the "is it still being written?" window: a
        browser download reports success early anyway (it writes a temp name
        and holds the file open), while a scanner that writes straight into
        the watch folder is the reason this exists at all. LOCAL-ONLY: not a
        catalog key, so it is neither pulled from nor pushed to GitHub.
        """
        try:
            value = float(self._machine_value(
                "popup_delay_seconds", DEFAULT_CONFIG["popup_delay_seconds"]))
        except (TypeError, ValueError):
            return float(DEFAULT_CONFIG["popup_delay_seconds"])
        if value != value:  # NaN
            return float(DEFAULT_CONFIG["popup_delay_seconds"])
        return max(0.0, min(10.0, value))

    @property
    def ocr_reasoning_effort(self) -> str:
        """Deprecated spelling of :attr:`ocr_thinking` (kept for old callers)."""
        return self.ocr_thinking

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

    @property
    def client_aliases(self) -> Dict[str, str]:
        """Return a copy of the {source client -> mapped client} map.

        Set with :meth:`set_client_alias`; OCR/save resolution is
        :meth:`resolve_client`.
        """
        aliases = self.load().get("client_aliases", {})
        return {str(k): str(v) for k, v in aliases.items()
                if isinstance(v, str)}

    @property
    def site_aliases(self) -> Dict[str, str]:
        """Return a copy of the {source site -> mapped site} map."""
        aliases = self.load().get("site_aliases", {})
        return {str(k): str(v) for k, v in aliases.items()
                if isinstance(v, str)}

    # ------------------------------------------------------------------
    # Mutators (each persists to disk)
    # ------------------------------------------------------------------
    def set_watch_directory(self, value: str) -> None:
        """Set where THIS person's downloads land (per-user on a shared install)."""
        self._set_machine_value("watch_directory", value)

    def set_root_directory(self, value: str) -> None:
        self._set_machine_value("root_directory", value)

    def set_local_setting(self, key: str, value: Any) -> None:
        """Write a per-person setting, whatever the install mode.

        Used by the watch-folder chooser: the answer is remembered for this
        Windows account even on a normal install, so a later switch to the
        shared server folder keeps it.
        """
        self._local.set(key, value)

    def set_auto_start(self, value: bool) -> None:
        """Turn "launch at Windows login" on/off (tray menu) and persist it.

        Written to the local config.json (per person on a shared install) so the
        choice survives restarts; the startup helper installs/removes the actual
        Windows entries on this machine.
        """
        self._set_machine_value("auto_start", bool(value))

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
            data = self.load()
            clients = data.setdefault("clients", {})
            canonical = find_near_name(list(clients.keys()), client)
            if canonical is not None:
                if canonical != client:
                    print(f"[config] client '{client}' is the same client as '{canonical}' — reusing existing name")
                return canonical
            clients[client] = list(sites or [])
            # An explicit re-add WINS over an earlier move-away tombstone.
            removed = data.get("removed_clients")
            if isinstance(removed, list):
                removed[:] = [r for r in removed
                              if str(r).strip().lower() != client.lower()]
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
        client = (client or "").strip()
        if not site:
            return ""
        if not client:
            # A site belongs to a client: never create an empty-named client
            # key (the popup asks for the Client before it offers Add Site).
            return ""
        if is_designator_only(site):
            # "Tower B", "Wing C", "T-9/10" — a bare unit designator carries
            # no place name and must NEVER become a site in config.json,
            # regardless of which caller adds it (OCR, the Save path, or the
            # typed Add-New-Site flow). Callers keep the typed value in the
            # field; only the catalog is protected here.
            return ""
        changed = False
        with self._lock:
            data = self.load()
            clients = data.setdefault("clients", {})
            key = self._canonical_key(clients, client)
            if key == client and key not in clients:
                # No exact (case-insensitive) client match — reuse a
                # near-same client instead of creating a duplicate.
                near = find_near_name(list(clients.keys()), client)
                if near is not None:
                    key = str(near)
            if key not in clients:
                # This save creates the client: an explicit re-add WINS over
                # an earlier move-away tombstone.
                removed = data.get("removed_clients")
                if isinstance(removed, list):
                    removed[:] = [r for r in removed
                                  if str(r).strip().lower() != str(key).lower()]
            sites = clients.setdefault(key, [])
            # Clean-preferring match: the catalog may hold only a
            # designator-suffixed STRAY for this place ("Lodha Palava -Fire
            # Station"). find_near_site then answers with the CLEAN name,
            # which is not a catalog entry yet — store it as one below.
            canonical = find_near_site(list(sites), site)
            if canonical is not None and any(
                    str(s).strip().lower() == str(canonical).strip().lower()
                    for s in sites):
                if canonical != site:
                    print(f"[config] site '{site}' is the same site as '{canonical}' — reusing existing name")
                return canonical
            if canonical is not None:
                effective = str(canonical)
            else:
                # A trailing unit designator ("Raymond Premium T-B" = Tower B,
                # "Kalpataru Elitus Tower B", "X Wing C") never becomes part of
                # a NEW site name — the designator is not the place, so the
                # stored site is "Raymond Premium" ("T-B is for Tower B so it
                # shouldn't be putting T-B in Site").
                effective = _strip_trailing_dash_designator(site)
            if effective != site:
                canonical = find_near_site(list(sites), effective)
                if canonical is not None:
                    if canonical != site:
                        print(f"[config] site '{site}' is the same site as '{canonical}' — reusing existing name")
                    return canonical
            sites.append(effective)
            self.save()
            changed = True
        if changed:
            self._mark_push_pending(reason=f"add site '{effective}' to '{key}'")
        return effective

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

    # ------------------------------------------------------------------
    # Name MAPPING (the 🗺 Map buttons): source name -> target name
    # ------------------------------------------------------------------
    def _set_alias(self, key: str, source: str, target: str, label: str) -> bool:
        """Shared implementation of set_client_alias/set_site_alias.

        Maps *source* -> *target* in the ``key`` alias dict and persists.
        Keys are unique case-insensitively: setting an alias whose source
        already exists (any casing) updates that entry instead of adding a
        duplicate. Push is deferred (a file must be saved first).
        """
        source = (source or "").strip()
        target = (target or "").strip()
        if not source or not target:
            return False
        changed = False
        with self._lock:
            data = self.load()
            aliases = data.get(key)
            if not isinstance(aliases, dict):
                aliases = {}
                data[key] = aliases
            canon = source
            for k in list(aliases):
                if str(k).lower() == source.lower():
                    canon = str(k)
                    break
            if aliases.get(canon) != target:
                aliases[canon] = target
                self.save()
                changed = True
        if changed:
            self._mark_push_pending(reason=f"map {label} '{source}' -> '{target}'")
        return changed

    def set_client_alias(self, source: str, target: str) -> bool:
        """Map the *source* client name to the *target* client name.

        Whenever OCR (or a saved popup) produces the source name, it is
        switched to the target. Returns True when the mapping changed.
        """
        return self._set_alias("client_aliases", source, target, "client")

    def set_site_alias(self, source: str, target: str) -> bool:
        """Map the *source* site name to the *target* site name (see
        :meth:`set_client_alias`; sites are mapped globally, not per-client)."""
        return self._set_alias("site_aliases", source, target, "site")

    def _remove_alias(self, key: str, source: str, label: str) -> bool:
        source = (source or "").strip()
        if not source:
            return False
        changed = False
        with self._lock:
            aliases = self.load().get(key)
            if isinstance(aliases, dict):
                for k in list(aliases):
                    if str(k).lower() == source.lower():
                        del aliases[k]
                        self.save()
                        changed = True
                        break
        if changed:
            self._mark_push_pending(reason=f"unmap {label} '{source}'")
        return changed

    def remove_client_alias(self, source: str) -> bool:
        """Delete the client mapping whose source is *source* (any casing)."""
        return self._remove_alias("client_aliases", source, "client")

    def remove_site_alias(self, source: str) -> bool:
        """Delete the site mapping whose source is *source* (any casing)."""
        return self._remove_alias("site_aliases", source, "site")

    def _resolve_alias(self, key: str, name: str) -> Optional[str]:
        """The mapped target for *name* in the ``key`` alias dict, else None.

        Exact case-insensitive source match first, then the near-match rule
        against the alias sources — so OCR's slightly different re-spelling
        ("kalpataru elit" for a "Kalpataru Elitus" alias key) still hits
        the mapping. See :func:`find_near_name` for the near-match contract.
        """
        name = (name or "").strip()
        if not name:
            return None
        aliases = self.load().get(key)
        if not isinstance(aliases, dict) or not aliases:
            return None
        for k, v in aliases.items():
            if str(k).strip().lower() == name.lower():
                return str(v)
        near = find_near_name([str(k) for k in aliases], name)
        if near is not None:
            return str(aliases.get(near))
        return None

    def resolve_client(self, name: str) -> Optional[str]:
        """The client that *name* is mapped to (an alias target), else None."""
        return self._resolve_alias("client_aliases", name)

    def resolve_site(self, name: str) -> Optional[str]:
        """The site that *name* is mapped to (an alias target), else None."""
        return self._resolve_alias("site_aliases", name)

    @property
    def material_aliases(self) -> Dict[str, str]:
        """Return a copy of the {goods word -> material name} map.

        A word the "Description of Goods" column shows ("nuts", "bolts")
        that is NOT a catalog material name maps to the material to select
        instead (e.g. "nuts" -> "Screw"). Set with
        :meth:`set_material_alias`; the popup's goods matcher applies it.
        """
        aliases = self.load().get("material_aliases", {})
        return {str(k): str(v) for k, v in aliases.items()
                if isinstance(v, str)}

    def set_material_alias(self, source: str, target: str) -> bool:
        """Map the *source* goods word (e.g. "nuts") to the *target* material.

        From the next OCR read, whenever the goods description mentions the
        source word, the target material is pre-selected. Returns True when
        the mapping changed; push is deferred (a file must be saved first).
        """
        return self._set_alias("material_aliases", source, target, "material")

    def remove_material_alias(self, source: str) -> bool:
        """Delete the material mapping whose source is *source* (any casing)."""
        return self._remove_alias("material_aliases", source, "material")

    def resolve_material(self, name: str) -> Optional[str]:
        """The material that the goods word *name* maps to, else None.

        Exact case-insensitive source match first, then near-match against
        the alias sources ("nut" for a "nuts" key still hits the mapping).
        """
        return self._resolve_alias("material_aliases", name)
