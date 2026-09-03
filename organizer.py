"""Directory routing & file distribution for FilePicker.

Given the metadata chosen in the popup, a completed file is copied (once) into
the destination folder::

    [root]/[Company]/[Client]/[Site]/[Doc Type]/[Received or Submitted]/[Formatted Filename]

And, when the Doc Type is ``DC``, an extra copy is placed into::

    [root]/[Company]/All DC/[Received or Submitted]/[Formatted Filename]
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import filename as fn

# Organize runs on worker threads (one per save) while the popup loop already
# shows the next file. Without a lock, two files with identical metadata and
# the same serial race each other in resolve_collision() — both see the
# target as free and the second copy silently overwrites the first.
_ORGANIZE_LOCK = threading.Lock()


@dataclass
class OrganizeResult:
    """Outcome of routing a file to its destination(s)."""

    destinations: List[Path] = field(default_factory=list)
    skipped: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.errors and not self.skipped


@dataclass
class OrganizeRequest:
    """Metadata describing how a file should be organized."""

    source: Path
    company: str            # top-level company folder
    client: str             # client that owns the site
    site: str
    doc_type: str
    materials: List[str]           # selected material *names*
    materials_map: dict            # name -> shortcode
    serial: str
    status: str                    # "Received" or "Submitted"
    root: Path
    replace: bool = False          # overwrite existing destination files
    initials_map: Optional[dict] = None   # company name -> initials override


def _destination_for(root: Path, company: str, client: str, site: str,
                     doc_type: str, status: str) -> Path:
    return (
        root
        / fn.sanitize(company)
        / fn.sanitize(client)
        / fn.sanitize(site)
        / fn.sanitize(doc_type)
        / fn.sanitize(status)
    )


def organize(request: OrganizeRequest) -> OrganizeResult:
    """Copy the source file into all required destination folders.

    The original file is left untouched here; callers decide whether to delete
    the watch-folder original after a successful run.
    """
    result = OrganizeResult()
    with _ORGANIZE_LOCK:  # serialise collision resolution + copies
        _organize_locked(request, result)
    return result


def _organize_locked(request: OrganizeRequest, result: OrganizeResult) -> None:

    if not request.source.exists():
        result.errors.append(f"Source file not found: {request.source}")
        return result

    ext = request.source.suffix.lstrip(".").lower()
    base_name = fn.build_filename(
        company=request.company,
        doc_type=request.doc_type,
        site_name=request.site,
        selected_materials=request.materials,
        materials_map=request.materials_map,
        serial=request.serial,
        extension=ext,
        initials_map=request.initials_map,
    )

    def place_copy(dest_dir: Path) -> Optional[Path]:
        """Copy the source into ``dest_dir`` (handling collisions)."""
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(f"Cannot create {dest_dir}: {exc}")
            return None
        safe_name = fn.resolve_collision(dest_dir, base_name, request.replace)
        target = dest_dir / safe_name
        try:
            shutil.copy2(request.source, target)
        except OSError as exc:
            result.errors.append(f"Cannot copy to {target}: {exc}")
            return None
        result.destinations.append(target)
        return target

    # --- Destination folder (single copy; no per-material subfolders) ----
    dest_dir = _destination_for(
        request.root, request.company, request.client,
        request.site, request.doc_type, request.status,
    )
    if place_copy(dest_dir) is None and result.errors:
        return result

    # --- Global "All DC" folder (only when Doc Type == "DC") ------------
    if request.doc_type.strip().upper() == "DC":
        all_dc_dir = request.root / fn.sanitize(request.company) / "All DC" / fn.sanitize(request.status)
        place_copy(all_dc_dir)

    return result
