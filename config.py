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
        "Aluminium": "A",
        "Carbon": "C",
        "Stainless Steel": "SS",
        "Mild Steel": "MS",
        "Galvanized Iron": "GI",
    },
    # NOTE: material codes are suffixed with "1" at filename time
    # (A -> A1, SS -> SS1) so single-letter tags are searchable.
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
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            # Fall back to defaults but never crash the watcher.
            self._data = deepcopy(DEFAULT_CONFIG)
            print(f"[config] Could not read {self.path}: {exc}")

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
        Returns True if anything changed and was saved.
        """
        with self._lock:
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

        Called automatically after Add Site/Company/Material. Merges the local
        catalog (companies, clients, etc.) into the current GitHub file so
        concurrent edits from two machines are unioned, not lost. Returns True
        on success.

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
        pushed = False
        with self._lock:
            companies = self.load().setdefault("companies", [])
            if not self._ci_matches(companies, company):
                companies.append(company)
                self.save()
                pushed = True
        if pushed:
            self._push_async(reason=f"FilePicker: add company '{company}'")

    def add_client(self, client: str, sites: Optional[List[str]] = None) -> None:
        pushed = False
        with self._lock:
            clients = self.load().setdefault("clients", {})
            if not self._ci_matches(clients.keys(), client):
                clients[client] = list(sites or [])
                self.save()
                pushed = True
        if pushed:
            self._push_async(reason=f"FilePicker: add client '{client}'")

    def add_site(self, client: str, site: str) -> None:
        """Add a new site under ``client``; create the client if needed."""
        pushed = False
        with self._lock:
            clients = self.load().setdefault("clients", {})
            key = self._canonical_key(clients, client)
            sites = clients.setdefault(key, [])
            if not self._ci_matches(sites, site):
                sites.append(site)
                self.save()
                pushed = True
        if pushed:
            self._push_async(reason=f"FilePicker: add site '{site}' to '{client}'")

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
            self._push_async(reason=f"FilePicker: add material '{name}'")

    def add_doc_type(self, doc_type: str) -> None:
        pushed = False
        with self._lock:
            doc_types = self.load().setdefault("doc_types", [])
            if not self._ci_matches(doc_types, doc_type):
                doc_types.append(doc_type)
                self.save()
                pushed = True
        if pushed:
            self._push_async(reason=f"FilePicker: add doc type '{doc_type}'")
