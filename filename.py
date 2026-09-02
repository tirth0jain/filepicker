"""Filename formatting rules for FilePicker.

The generated filename strictly follows the format::

    {Company}-{Doc Type}-{Financial Year}-{Site Name}-{Serial}-{Material Shortcodes}-{Status}.{ext}

e.g. ``Acme-DC-26-27-Site 1 - Mumbai-0001-A+C-Received.pdf``
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

# Characters that are illegal in Windows file names.
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


def sanitize(value: str, default: str = "Untitled") -> str:
    """Remove illegal Windows filename characters and collapse whitespace.

    Also strips leading/trailing dots/spaces (which Windows silently drops or
    treats as invalid) and empty segments.
    """
    cleaned = _ILLEGAL_CHARS.sub(" ", value)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    # Windows disallows names ending in a dot or space.
    cleaned = cleaned.rstrip(" .")
    # Trim, and collapse repeated spaces.
    cleaned = " ".join(cleaned.split())
    return cleaned or default


def financial_year(now: Optional[datetime.date] = None) -> str:
    """Compute the Indian Financial Year (April 1 -> March 31).

    Returns a string of the form ``YY-YY``, e.g. ``26-27`` for Aug 2026.

    - If current month >= April: ``YY-(YY+1)``
    - If current month < April: ``(YY-1)-YY``
    """
    today = now or datetime.date.today()
    year = today.year % 100
    if today.month >= 4:
        return f"{year:02d}-{(year + 1) % 100:02d}"
    return f"{(year - 1) % 100:02d}-{year:02d}"


def material_shortcodes(selected_names, materials_map) -> str:
    """Join the shortcodes of the selected materials with ``+``.

    Unknown material names are mapped to their first letter so nothing breaks.
    Every code has ``1`` appended (e.g. ``A`` -> ``A1``, ``SS`` -> ``SS1``)
    so the tag is not a single common letter.
    """
    codes = []
    for name in selected_names:
        code = materials_map.get(name)
        if not code:
            code = sanitize(name)[:1] or "?"
        # Material codes are always suffixed with "1" (e.g. A -> A1, SS -> SS1).
        # Avoid doubling if the stored code already ends with "1".
        if not code.endswith("1"):
            code = f"{code}1"
        codes.append(code)
    return "+".join(codes)


def company_initials(company: str, initials_map: Optional[dict] = None) -> str:
    """Return the initials used in the filename for a company.

    Prefers an explicit mapping (``initials_map`` = {company: initials}) so the
    user can override, then falls back to the first letter of each word:

    - "Ruby Steel" -> "RS"
    - "Ruby Steel Railings & Facades" -> "RSRF"
    - "RSB" manual override -> "RSB"
    """
    if initials_map:
        explicit = initials_map.get(company)
        if explicit:
            return sanitize(explicit)
    words = [w for w in re.split(r"[\s&\-/]+", company) if w]
    return sanitize("".join(w[0] for w in words).upper()) or sanitize(company)[:1].upper()


def build_filename(
    company: str,
    doc_type: str,
    site_name: str,
    selected_materials,
    materials_map: dict,
    serial: str,
    status: str,
    extension: str,
    now: Optional[datetime.date] = None,
    initials_map: Optional[dict] = None,
) -> str:
    """Assemble the fully formatted file name.

    ``status`` should be either ``"Received"`` or ``"Submitted"``.
    ``extension`` should be provided without a leading dot (e.g. ``"pdf"``).
    ``initials_map`` optionally maps a company name to its short initials used
    in the filename (otherwise initials are derived from the name).
    """
    fy = financial_year(now)
    codes = material_shortcodes(selected_materials, materials_map)
    company_code = company_initials(company, initials_map)

    stem = "-".join(
        [
            company_code,
            sanitize(doc_type),
            fy,
            sanitize(site_name),
            sanitize(serial),
            codes,
            sanitize(status),
        ]
    )
    ext = extension.lstrip(".") if extension else ""
    return f"{stem}.{ext}" if ext else stem


def resolve_collision(destination_dir, filename: str, replace: bool = False) -> str:
    """Resolve a name clash at ``destination_dir``.

    If ``replace`` is True the original name is returned so callers can
    overwrite. Otherwise append ``_1``, ``_2``, ... before the extension so no
    existing file is ever clobbered. Handles names with and without an
    extension (e.g. ``report.pdf`` -> ``report_1.pdf``, ``report`` ->
    ``report_1``).
    """
    target = destination_dir / filename
    if replace or not target.exists():
        return filename

    if "." in filename:
        stem, _, ext = filename.rpartition(".")
        ext = "." + ext
    else:
        stem, ext = filename, ""
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{ext}"
        if not (destination_dir / candidate).exists():
            return candidate
        counter += 1
