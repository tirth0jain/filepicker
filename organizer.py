"""Directory routing & file distribution for FilePicker.

Given the metadata chosen in the popup, a completed file is copied (once) into
the destination folder::

    [root]/[Company]/[Client]/[Site]/[Doc Type]/[Received or Submitted]/[Formatted Filename]

And, when the Doc Type is ``DC``, an extra copy is placed into::

    [root]/[Company]/All DC/[Received or Submitted]/[Formatted Filename]
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import filename as fn


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
        status=request.status,
        extension=ext,
    )

    # --- Destination folder (single copy; no per-material subfolders) ----
    dest_dir = _destination_for(
        request.root, request.company, request.client,
        request.site, request.doc_type, request.status,
    )
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Cannot create {dest_dir}: {exc}")
        return result

    safe_name = fn.resolve_collision(dest_dir, base_name, request.replace)
    try:
        shutil.copy2(request.source, dest_dir / safe_name)
        result.destinations.append(dest_dir / safe_name)
    except OSError as exc:
        result.errors.append(f"Cannot copy to {dest_dir / safe_name}: {exc}")

    # --- Global "All DC" folder (only when Doc Type == "DC") ------------
    if request.doc_type.strip().upper() == "DC":
        all_dc_dir = request.root / fn.sanitize(request.company) / "All DC" / fn.sanitize(request.status)
        try:
            all_dc_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.errors.append(f"Cannot create {all_dc_dir}: {exc}")
        else:
            safe_name = fn.resolve_collision(all_dc_dir, base_name, request.replace)
            try:
                shutil.copy2(request.source, all_dc_dir / safe_name)
                result.destinations.append(all_dc_dir / safe_name)
            except OSError as exc:
                result.errors.append(f"Cannot copy to {all_dc_dir / safe_name}: {exc}")

    return result
