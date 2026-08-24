"""Tag search for FilePicker.

Windows Search often doesn't index network drives (e.g. ``Z:\\Sorted``), so
searching Explorer for "aluminium" finds nothing even though the files carry
the tag in their metadata. This module gives FilePicker its own tag index so
the user can search by tag inside the app — instantly, offline, regardless of
Windows indexing.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Set

try:
    from config import default_config_path

    _INDEX_FILE = default_config_path().with_name("tag_index.json")
except Exception:
    _INDEX_FILE = Path(__file__).resolve().parent / "tag_index.json"

_LOCK = threading.RLock()


def _clean(tags) -> List[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        parts = [t.strip() for t in tags.split(",")]
    else:
        parts = [str(t).strip() for t in tags]
    seen: Set[str] = set()
    out: List[str] = []
    for p in parts:
        low = p.lower()
        if p and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _load() -> Dict[str, List[str]]:
    """Load the index: {tag(lower) -> [file paths]}."""
    try:
        with open(_INDEX_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save(index: Dict[str, List[str]]) -> None:
    try:
        tmp = _INDEX_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _INDEX_FILE)
    except OSError as exc:
        print(f"[tagsearch] could not write index: {exc}")


def add_entry(path: Path, tags) -> None:
    """Record ``tags`` for ``path`` in the index."""
    cleaned = _clean(tags)
    if not cleaned:
        return
    with _LOCK:
        index = _load()
        for tag in cleaned:
            files = index.setdefault(tag, [])
            spath = str(path)
            if spath not in files:
                files.append(spath)
        _save(index)


def remove_entry(path: Path) -> None:
    """Remove a file from every tag it is indexed under."""
    with _LOCK:
        index = _load()
        target = str(path)
        for tag in list(index.keys()):
            files = [f for f in index[tag] if f != target]
            if files:
                index[tag] = files
            else:
                del index[tag]
        _save(index)


def search(query: str, limit: int = 50) -> List[dict]:
    """Return files whose tags match ``query`` (case-insensitive substring).

    Each result is ``{"path": ..., "tags": [...]}``.
    """
    index = _load()
    q = query.strip().lower()
    if not q:
        return []
    hits: Dict[str, Set[str]] = {}
    for tag, files in index.items():
        if q in tag:
            for f in files:
                hits.setdefault(f, set()).add(tag)
    results = []
    for path, tags in hits.items():
        if Path(path).exists():
            results.append({"path": path, "tags": sorted(tags)})
    results.sort(key=lambda r: r["path"])
    return results[:limit]


def all_tags() -> List[str]:
    """Every tag currently in the index (sorted)."""
    return sorted(_load().keys())
