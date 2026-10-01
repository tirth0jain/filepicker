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
    # Financial year for the filename, read from the document's "Delivery
    # Note No." ("RS/DC/25-26/123" -> "25-26") and editable in the popup.
    # None (or an invalid value) means "use today's financial year".
    fy: Optional[str] = None


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


def output_paths(request: OrganizeRequest) -> List[Path]:
    """The exact destination paths :func:`organize` would write to.

    No copies are made. The controller uses this to de-dup *before* writing:
    when one of these paths already exists (same filename as the one the
    program is about to output), the user is asked whether to skip the new
    file or replace the old one. The primary path is always first; the
    ``All DC`` extra copy (DC only) comes second.
    """
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
        fy=request.fy,
    )
    paths = [
        _destination_for(
            request.root, request.company, request.client,
            request.site, request.doc_type, request.status,
        )
        / base_name,
    ]
    if request.doc_type.strip().upper() == "DC":
        paths.append(
            request.root
            / fn.sanitize(request.company)
            / "All DC"
            / fn.sanitize(request.status)
            / base_name
        )
    return paths


def serial_scan_dirs(request: OrganizeRequest) -> List[Path]:
    """The folders a same-serial duplicate can be hiding in.

    The "respective Doc Type folder" — ``.../<Company>/<Client>/<Site>/<Doc
    Type>/`` — is scanned as a WHOLE, so a document filed as *Received* is
    found when the same one arrives as *Submitted* (the status is a folder, not
    a document property). For a ``DC`` the ``All DC`` copy of the same document
    counts too: it holds the same serial for the same company.
    """
    root = Path(request.root)
    dirs = [
        root
        / fn.sanitize(request.company)
        / fn.sanitize(request.client)
        / fn.sanitize(request.site)
        / fn.sanitize(request.doc_type),
    ]
    if request.doc_type.strip().upper() == "DC":
        dirs.append(root / fn.sanitize(request.company) / "All DC")
    return dirs


def serial_duplicate_paths(request: OrganizeRequest) -> List[Path]:
    """Existing files that already carry this document's serial number.

    The name-based de-dup in :func:`output_paths` only catches a file with the
    EXACT same name. The same delivery note scanned again often lands on a
    different name — a material picked differently, a wrong financial year, a
    re-spelled site, or the other status folder — so the serial number is
    checked on its own, inside the Doc Type folder the document is going to.

    Returns the matching files (sorted, stable), empty when there is nothing to
    warn about. It never touches the disk.
    """
    serial = fn.sanitize(str(request.serial or "")).strip()
    if not serial:
        return []      # nothing to compare on
    found: List[Path] = []
    for folder in serial_scan_dirs(request):
        try:
            if not folder.is_dir():
                continue
            for path in folder.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                if fn.filename_has_serial(path.name, serial):
                    found.append(path)
        except OSError as exc:
            print(f"[organizer] serial duplicate scan failed in {folder}: {exc}")
    return sorted(found)


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

    targets = output_paths(request)
    base_name = targets[0].name

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

    # --- Destination folder(s): primary first, then the "All DC" copy. ----
    if place_copy(targets[0].parent) is None and result.errors:
        return result
    for target in targets[1:]:
        place_copy(target.parent)

    return result
