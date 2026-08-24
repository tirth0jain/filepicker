"""Configuration management for FilePicker.

Loads and persists a ``config.json`` file. The config is written next to the
application (the directory containing this module) so it travels with the
utility and survives reinstalls. All dynamic changes made from the popup UI
(companies, clients, sites, materials, doc types) are saved back to this file.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

# Remote live config — single source of truth for clients/sites.
# Every popup fetches this so all users see the same data instantly.
GITHUB_CONFIG_URL = "https://raw.githubusercontent.com/tirth0jain/filepicker/main/config.json"

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

    def sync_from_github(self, timeout: float = 5.0) -> bool:
        """Fetch and apply the live config if it changed. Returns True if updated."""
        remote = self.fetch_github_config(timeout=timeout)
        if remote is None:
            return False
        return self.apply_github_config(remote)

    def apply_github_config(self, remote: Dict[str, Any]) -> bool:
        """Merge the live GitHub config into the local one.

        Only the shared catalog keys are overwritten (companies, clients,
        materials, doc_types, company_initials). Local paths
        (watch_directory, root_directory, auto_start) are never clobbered.
        Returns True if anything changed and was saved.
        """
        with self._lock:
            changed = False
            for key in ("companies", "company_initials", "clients", "materials", "doc_types"):
                if key in remote and remote[key] != self._data.get(key):
                    self._data[key] = remote[key]
                    changed = True
            if changed:
                self.save()
            return changed

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
        with self._lock:
            companies = self.load().setdefault("companies", [])
            if not self._ci_matches(companies, company):
                companies.append(company)
                self.save()

    def add_client(self, client: str, sites: Optional[List[str]] = None) -> None:
        with self._lock:
            clients = self.load().setdefault("clients", {})
            if not self._ci_matches(clients.keys(), client):
                clients[client] = list(sites or [])
                self.save()

    def add_site(self, client: str, site: str) -> None:
        """Add a new site under ``client``; create the client if needed."""
        with self._lock:
            clients = self.load().setdefault("clients", {})
            key = self._canonical_key(clients, client)
            sites = clients.setdefault(key, [])
            if not self._ci_matches(sites, site):
                sites.append(site)
                self.save()

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
        with self._lock:
            materials = self.load().setdefault("materials", {})
            materials[name] = shortcode
            self.save()

    def add_doc_type(self, doc_type: str) -> None:
        with self._lock:
            doc_types = self.load().setdefault("doc_types", [])
            if not self._ci_matches(doc_types, doc_type):
                doc_types.append(doc_type)
                self.save()
