"""Filename formatting rules for FilePicker.

The generated filename strictly follows the format::

    {Company}-{Doc Type}-{Financial Year}-{Serial}-{Site Name}-{Material Shortcodes}.{ext}

e.g. ``Acme-DC-26-27-0001-Site 1 - Mumbai-AL1+MS1.pdf``

The Received/Submitted status is deliberately NOT part of the filename — it is
reflected only in the destination folder structure
(``.../<Doc Type>/<Received or Submitted>/``).
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

    This is only the DEFAULT for a popup: the financial year that belongs in
    the filename is the one printed in the document's "Delivery Note No."
    ("RS/DC/25-26/123" -> "25-26"), which is read by OCR and passed to
    :func:`build_filename` as ``fy``. See :func:`normalize_fy`.
    """
    today = now or datetime.date.today()
    year = today.year % 100
    if today.month >= 4:
        return f"{year:02d}-{(year + 1) % 100:02d}"
    return f"{(year - 1) % 100:02d}-{year:02d}"


# A financial year as people write it: "25-26", "2025-26", "25/26", "2025/2026".
_FY_RE = re.compile(r"^\s*(\d{2}|\d{4})\s*[-/]\s*(\d{2}|\d{4})\s*$")
# The same pair inside a longer reference: "RS/DC/25-26/123", "RS-DC-25-26-7",
# "2026-27-0144", "2025-2026". Either side may be written as a full year; the
# lookarounds keep it from matching inside a longer digit run ("1234-56").
_FY_IN_TEXT_RE = re.compile(r"(?<![\d])((?:20)?\d{2})\s*-\s*((?:20)?\d{2})(?![\d])")


def normalize_fy(value) -> Optional[str]:
    """Normalise a financial year to ``YY-YY``, else None.

    Accepts "25-26", "25/26", "2025-26", "2025/2026" (and stray spaces) and
    returns "25-26". The two years must be CONSECUTIVE ("25-26" is a financial
    year, "25-27" is a typo), otherwise None is returned and the caller falls
    back to the current financial year.
    """
    match = _FY_RE.match(str(value or ""))
    if not match:
        return None
    start = int(match.group(1)) % 100
    end = int(match.group(2)) % 100
    if end != (start + 1) % 100:
        return None
    return f"{start:02d}-{end:02d}"


def fy_from_text(value) -> Optional[str]:
    """The financial year inside a reference, e.g. "RS/DC/25-26/123" -> "25-26".

    Used for the document's "Delivery Note No." and for the download's file
    name (which usually carries the same reference). Returns None when the
    text holds no plausible year pair.
    """
    text = str(value or "")
    direct = normalize_fy(text)
    if direct:
        return direct
    for match in _FY_IN_TEXT_RE.finditer(text):
        candidate = normalize_fy(f"{match.group(1)}-{match.group(2)}")
        if candidate:
            return candidate
    return None


def fy_options(now: Optional[datetime.date] = None, include=None,
               back: int = 2, forward: int = 1) -> list:
    """The financial years a popup offers, newest-relevant first.

    The document's own year (``include``) comes first when it is known, then
    the current one, then the previous ``back`` years and the next ``forward``
    year — e.g. for Aug 2026: ["26-27", "25-26", "24-25", "27-28"] (with
    "25-26" first when the document says so).
    """
    today = now or datetime.date.today()
    current_start = today.year % 100 if today.month >= 4 else (today.year - 1) % 100
    years = []
    wanted = []
    known = normalize_fy(include) if include else None
    if known:
        wanted.append(known)
    wanted.append(f"{current_start:02d}-{(current_start + 1) % 100:02d}")
    for offset in range(1, back + 1):
        start = (current_start - offset) % 100
        wanted.append(f"{start:02d}-{(start + 1) % 100:02d}")
    for offset in range(1, forward + 1):
        start = (current_start + offset) % 100
        wanted.append(f"{start:02d}-{(start + 1) % 100:02d}")
    for value in wanted:
        if value not in years:
            years.append(value)
    return years


def material_code(name: str, stored: Optional[str] = None) -> str:
    """The 2-letter shortcode for a material (e.g. Aluminium -> "AL").

    ``stored`` is the code from the config: codes that are already at least
    two letters (SS, GI, ...) are returned verbatim; single-letter leftovers
    from older configs ("A" for Aluminium) are expanded to two letters
    derived from the material name. Unknown/empty codes are derived from the
    name as well, so every code is exactly two letters.
    """
    code = (stored or "").strip().upper()
    if len(code) >= 2:
        return code
    words = [w for w in re.split(r"[\s&/_-]+", name) if w]
    if not words:
        return code or "XX"
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    word = words[0]
    if len(word) >= 2:
        return word[:2].upper()
    return (word + "X").upper()


def material_shortcodes(selected_names, materials_map) -> str:
    """Join the 2-letter shortcodes of the selected materials with ``+``.

    Unknown material names get a 2-letter code derived from the name so
    nothing breaks. Every code has ``1`` appended (e.g. ``AL`` -> ``AL1``,
    ``SS`` -> ``SS1``) so the tag is never a bare letter.
    """
    codes = []
    for name in selected_names:
        code = material_code(name, materials_map.get(name))
        # Material codes are always suffixed with "1" (e.g. AL -> AL1, SS -> SS1).
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
    extension: str,
    now: Optional[datetime.date] = None,
    initials_map: Optional[dict] = None,
    fy: Optional[str] = None,
) -> str:
    """Assemble the fully formatted file name.

    Parts always appear in this order:

        {Company}-{Doc Type}-{FY}-{Serial}-{Site Name}-{Material Shortcodes}.{ext}

    ``extension`` should be provided without a leading dot (e.g. ``"pdf"``).
    ``initials_map`` optionally maps a company name to its short initials used
    in the filename (otherwise initials are derived from the name).

    ``fy`` is the financial year the DOCUMENT belongs to — read from its
    "Delivery Note No." ("RS/DC/25-26/123" -> "25-26") and shown in the
    popup, so a note from the previous year is filed as 25-26 instead of
    whatever today's date would give. Anything that is not a consecutive
    year pair is ignored and :func:`financial_year` (today) is used instead,
    so a bad value can never reach the filename.

    The Received/Submitted ``status`` is intentionally absent: it lives only
    in the destination folder (``.../<Doc Type>/<Received or Submitted>/``).
    """
    fy = normalize_fy(fy) or financial_year(now)
    codes = material_shortcodes(selected_materials, materials_map)
    company_code = company_initials(company, initials_map)

    stem = "-".join(
        [
            company_code,
            sanitize(doc_type),
            fy,
            sanitize(serial),
            sanitize(site_name),
            codes,
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
