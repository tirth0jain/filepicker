"""Tag writing for FilePicker.

The filename only carries material *shortcodes* (e.g. ``A`` for Aluminium), so
searching Windows Explorer for "aluminium" finds nothing. To fix that, FilePicker
writes the user's tags (e.g. "Aluminium, DC, Site 1") into the file's own metadata
as searchable keywords. Explorer indexes these and lets you search the full word
even though the filename shows the shortcode.

Supported types:
- PDF        -> PDF document keywords (PyMuPDF)
- Excel      -> workbook core properties keywords (openpyxl / xlrd)
- Word       -> core properties keywords (python-docx)
- Images     -> EXIF/PNG keyword metadata (Pillow)

Any other type is left untouched (still organized, just not tagged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional


def _clean_tags(tags) -> List[str]:
    """Normalize a list/tuple/string of tags into a deduped, non-empty list."""
    if not tags:
        return []
    if isinstance(tags, str):
        parts = [t.strip() for t in tags.split(",")]
    else:
        parts = [str(t).strip() for t in tags]
    seen = set()
    out = []
    for p in parts:
        key = p.lower()
        if p and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _add_image_tags(path: Path, tags: List[str]) -> None:
    """Write tags into image metadata (PNG text chunks, EXIF XPKeywords)."""
    from PIL import Image

    try:
        if Image.open(path).format == "PNG":
            # PNG: write a tEXt chunk with the keywords (kept on re-save).
            from PIL import PngImagePlugin

            with Image.open(path) as img:
                pnginfo = PngImagePlugin.PngInfo()
                existing = (img.info.get("Description") or "").strip()
                merged = ", ".join(tags)
                if existing:
                    merged = f"{existing}, {merged}"
                pnginfo.add_text("Description", merged)
                buf = img.copy()
                buf.save(path, format="PNG", pnginfo=pnginfo)
            return
        # JPEG / TIFF etc: set EXIF XPKeywords.
        with Image.open(path) as img:
            exif = img.getexif()
            XPKEYWORDS = 40094
            exif[XPKEYWORDS] = "\u0000".join(tags) + "\u0000"
            exif_bytes = exif.tobytes()
            img.save(path, format=img.format, exif=exif_bytes)
    except Exception as exc:  # never fail the organize over metadata
        print(f"[tags] image tag failed for {path.name}: {exc}")


def _add_pdf_tags(path: Path, tags: List[str]) -> None:
    """Write tags into a PDF as document keywords."""
    try:
        import pymupdf  # PyMuPDF >= 1.24
    except ImportError:
        import fitz as pymupdf
    try:
        doc = pymupdf.open(str(path))
        metadata = dict(doc.metadata or {})
        current = metadata.get("keywords") or ""
        merged = ", ".join(tags)
        if current:
            merged = f"{current}, {merged}"
        metadata["keywords"] = merged
        doc.set_metadata(metadata)
        doc.saveIncr()  # incremental save keeps it light
        doc.close()
    except Exception as exc:
        print(f"[tags] pdf tag failed for {path.name}: {exc}")


def _add_excel_tags(path: Path, tags: List[str]) -> None:
    """Write tags into Excel workbook core properties."""
    ext = path.suffix.lower()
    try:
        if ext == ".xls":
            # xlrd can't write; xlwt would be needed. Skip legacy xls tagging.
            return
        import openpyxl
        wb = openpyxl.load_workbook(path)
        current = wb.properties.keywords or ""
        merged = ", ".join(tags)
        if current:
            merged = f"{current}, {merged}"
        wb.properties.keywords = merged
        wb.save(path)
    except Exception as exc:
        print(f"[tags] excel tag failed for {path.name}: {exc}")


def _add_word_tags(path: Path, tags: List[str]) -> None:
    """Write tags into a Word document's core properties."""
    try:
        from docx import Document
        doc = Document(str(path))
        cp = doc.core_properties
        current = cp.keywords or ""
        merged = ", ".join(tags)
        if current:
            merged = f"{current}, {merged}"
        cp.keywords = merged
        doc.save(str(path))
    except Exception as exc:
        print(f"[tags] word tag failed for {path.name}: {exc}")


def apply_tags(path: Path, tags) -> None:
    """Write ``tags`` into the file's metadata as searchable keywords.

    The destination file is edited in place (so Explorer can index the tags).
    Only supported formats are touched; failures are logged, never fatal.
    """
    cleaned = _clean_tags(tags)
    if not cleaned:
        return
    ext = path.suffix.lower()
    if ext in {".pdf"}:
        _add_pdf_tags(path, cleaned)
    elif ext in {".xlsx", ".xlsm", ".xls"}:
        _add_excel_tags(path, cleaned)
    elif ext in {".docx", ".doc"}:
        _add_word_tags(path, cleaned)
    elif ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp"}:
        _add_image_tags(path, cleaned)
    # Other types: no metadata slot, skip silently.
