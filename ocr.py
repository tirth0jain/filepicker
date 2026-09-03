"""OCR of delivery-note documents via the OpenCode Go vision model.

When ``enable_ocr`` is on, the popup sends the first page of a new download
(PDF or image) to the **DeepSeek V4 Flash Vision Exp** model on the OpenCode
Go catalog (`opencode.ai/zen/go/v1`, OpenAI-compatible API) with a prompt
that asks for:

    Company (Supplier) / Client (Buyer) / Site (Other References) /
    Serial Number (Delivery Note No.)

and pre-fills the popup fields from the returned table.

Credentials are resolved by :func:`config._read_opencode_token` — the same
key as the opencode CLI uses. Production machines put the key in
``opencode_token.txt`` next to the exe (or env ``FILEPICKER_OPENCODE_TOKEN``
/ ``OPENCODE_API_KEY``); dev machines fall back to opencode's own auth store.

This module intentionally has no UI and never raises: callers always get a
plain dict (``None`` values for fields the model could not determine) or
``None`` when the whole call failed.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from version import VERSION

# OpenCode Go catalog endpoint (OpenAI-compatible). Both the OpenCode Go
# subscription and the zen catalog share one key; the Go catalog is served
# under /zen/go/v1.
OCR_API_BASE = "https://opencode.ai/zen/go/v1"

# The vision model from the user's OpenCode Go subscription ("DeepSeek V4
# Flash Vision Exp"). Reasoning-heavy: needs a large max_tokens budget or it
# runs out of room before producing the answer table (see OCR_MAX_TOKENS).
OCR_MODEL = "deepseek-v4-flash-vision-exp"

# The model burns ~1500 tokens reasoning on a simple delivery note; 4096
# leaves room for harder documents without truncating the answer table.
OCR_MAX_TOKENS = 4096

# Total wall-clock budget for one OCR call (seconds). Vision + reasoning on a
# busy gateway can take a while; 120s keeps the popup snappy while allowing
# the model to finish.
OCR_TIMEOUT = 120

# Maximum image dimension sent to the model (pixels). Keeps the request
# small and fast without hurting text legibility.
OCR_MAX_IMAGE_DIM = 1600

# JPEG quality used when compressing the rendered page for the API call.
OCR_JPEG_QUALITY = 90

# Maximum number of vision calls run at the same time. When many files land
# at once, OCR is processed in batches of this size: as soon as one of the
# current calls finishes, the next queued file starts (so at most
# MAX_CONCURRENT_OCR requests are ever in flight).
MAX_CONCURRENT_OCR = 10

# Cloudflare in front of the OpenCode gateway blocks the default
# "Python-urllib" user agent (HTTP 403, error code 1010), so every request
# carries a browser-like application UA.
_UA = f"FilePicker/{VERSION} (Windows; DeliveryNote OCR)"

# The extraction prompt — verbatim from the feature spec (Serial Number added
# in 0.6.4: read from the "Delivery Note No." field, digits only, 1-4 digits.
# Site made strictly "Other References"-only in 0.6.6: the model must never
# substitute a "Reference No." / "Ref No." value for the missing site.)
OCR_PROMPT = """You are given a delivery note document. Extract the following information and present it in a table format:

1. Company (Supplier) - the company supplying the goods (e.g., Ruby Steel)
2. Client (Buyer) - the company being supplied to (e.g., Larsen and Toubro, Honest Shelters Pvt Ltd)
3. Site - ONLY the value of the field literally labelled "Other References" (e.g., Kalpataru Vivant (T-A), Palais Royal (Amenity), Lodha Regalia Tower 2)
4. Serial Number - the number in the "Delivery Note No." field (e.g., "RS/DC/26-27/6" -> 6, "RS/DC/26-27/55" -> 55)

Rules:
- Company is the supplier (from the "From" / "RUBY STEEL" section)
- Client is the buyer/consignee (from "Buyer (Bill to)" or "Consignee (Ship to)" section)
- Site MUST come ONLY from the field literally labelled "Other References". NEVER use "Reference No.", "Ref No.", "SR. No.", "Bill No.", "Invoice No.", "Delivery Note No.", "PO No." or any other field for Site
- If the document has no "Other References" field, leave the Site cell EMPTY (do not substitute any other value)
- Serial Number is the numeric part of the "Delivery Note No." value: digits only, 1-4 digits, usually the part after the last "/" (e.g. "RS/DC/26-27/6" -> 6, "RS/DC/26-27/55" -> 55)
- If the Delivery Note No. is not present, leave Serial Number empty
- Case insensitive, convert to Title Case

Output format:

| Role | Value |
|------|-------|
| Company (Supplier) | [Name] |
| Client (Buyer) | [Name] |
| Site (Other References) | [Name] |
| Serial Number (Delivery Note No.) | [Number] |"""

# Known-Sites section appended to the base prompt (see build_ocr_prompt).
# The model gets the current site catalog so a document that writes a site
# slightly differently ("sital baug") is resolved to the existing name
# ("Sital Baug") instead of becoming a duplicate site in the config.
_KNOWN_SITES_SECTION = """

Known Sites (the current site list from the app's config):
{sites}

Site matching rule (IMPORTANT): the "Other References" value in the document is
usually one of the Known Sites above written slightly differently — different
letters, spacing, punctuation, or with/without articles ("a"/"an"/"the"), or an
extra word like a brand name ("Lodha Shital Baug" vs "sital baug"). When the
value is the same place as one of the Known Sites, output the Known Site name
EXACTLY as listed above instead of the document's spelling. Only output a name
NOT on the list when it clearly matches no Known Site (e.g. a brand-new site)."""


def build_ocr_prompt(known_sites=None) -> str:
    """The OCR prompt, with the current known site names appended.

    ``known_sites`` is the list of site names already in the config (from all
    clients). When it is empty/None the bare :data:`OCR_PROMPT` is returned so
    the CLI and the default code path are unchanged.
    """
    if not known_sites:
        return OCR_PROMPT
    sites = [str(s).strip() for s in known_sites if str(s).strip()]
    if not sites:
        return OCR_PROMPT
    listed = "\n".join(f"- {s}" for s in sites)
    return OCR_PROMPT + _KNOWN_SITES_SECTION.format(sites=listed)

# Labels the model is asked to emit, mapped to our result keys. Matching is
# case-insensitive and tolerant of extra whitespace/backticks around the row.
_ROW_PATTERNS = {
    "company": re.compile(r"Company\s*\(Supplier\)", re.IGNORECASE),
    "client": re.compile(r"Client\s*\(Buyer\)", re.IGNORECASE),
    "site": re.compile(r"Site\s*\(Other\s*References\)", re.IGNORECASE),
    "serial": re.compile(
        # "Serial Number (Delivery Note No.)" / "Serial Number" /
        # "Delivery Note No." / "Delivery Note Number" / "Serial No."
        r"(?:Serial\s*(?:Number|No\.?)|Delivery\s*Note\s*(?:Number|No\.?))"
        r"\s*(?:\(\s*Delivery\s*Note\s*(?:Number|No\.?)\s*\))?",
        re.IGNORECASE,
    ),
}

# Values that mean "nothing found" — treated as absent.
_NULL_VALUES = {"", "-", "--", "n/a", "na", "none", "not found", "not available", "unknown"}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".tif", ".gif"}


def render_to_data_url(
    file_path: Path,
    max_dim: int = OCR_MAX_IMAGE_DIM,
    quality: int = OCR_JPEG_QUALITY,
) -> Optional[str]:
    """Render the first page of *file_path* to a JPEG data URL for the vision API.

    PDFs are rendered with PyMuPDF (first page, ~144 DPI); images are loaded
    with Pillow (first frame). Returns None for unsupported files or render
    errors — callers continue without OCR.
    """
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".pdf":
            return _pdf_to_data_url(file_path, max_dim=max_dim, quality=quality)
        if suffix in _IMAGE_EXTS:
            return _image_to_data_url(file_path, max_dim=max_dim, quality=quality)
        print(f"[ocr] unsupported file type for OCR: {file_path}")
    except Exception as exc:
        print(f"[ocr] could not render {file_path} for OCR: {exc}")
    return None


def _pdf_to_data_url(file_path: Path, max_dim: int, quality: int) -> Optional[str]:
    try:
        import pymupdf as fitz
    except ImportError:  # older PyMuPDF (<1.24) exposes the module as `fitz`
        import fitz  # type: ignore
    with fitz.open(str(file_path)) as doc:
        page = doc[0] if doc.page_count else None
        if page is None:
            return None
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        # PyMuPDF 1.24+ deprecated Pixmap.tobytes("png"); do the conversion via
        # Pillow so we always have a JPEG data URL regardless of version.
        img_bytes = pix.tobytes("png")
    return _pil_to_data_url(io.BytesIO(img_bytes), max_dim=max_dim, quality=quality)


def _image_to_data_url(file_path: Path, max_dim: int, quality: int) -> Optional[str]:
    from PIL import Image
    with Image.open(file_path) as img:
        img.seek(0)  # first frame of GIF/TIFF
        return _pil_to_data_url(img, max_dim=max_dim, quality=quality)


def _pil_to_data_url(image, max_dim: int, quality: int) -> str:
    from PIL import Image

    if not isinstance(image, Image.Image):
        with Image.open(image) as img:
            pil = img.copy()
    else:
        pil = image

    # Flatten transparency onto white so JPEG compression is lossless-ish for
    # scans and keeps no alpha channel.
    if pil.mode in ("RGBA", "LA", "P"):
        rgba = pil.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        pil = bg
    elif pil.mode != "RGB":
        pil = pil.convert("RGB")

    if max(pil.size) > max_dim:
        pil.thumbnail((max_dim, max_dim), Image.LANCZOS)

    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _clean_serial(value: str) -> str:
    """Normalise the OCR value for the Serial Number to plain digits.

    The model is asked for the numeric part of "Delivery Note No." but may
    return the whole reference (e.g. "RS/DC/26-27/55"). The serial is the
    number after the last "/"; fall back to the last 1-4 digit token
    anywhere in the value. Returns "" when nothing usable is found.
    """
    tail = value.rsplit("/", 1)[-1]
    nums = re.findall(r"\b\d{1,4}\b", tail)
    if nums:
        return nums[-1]
    nums = re.findall(r"\b\d{1,4}\b", value)
    if nums:
        return nums[-1]
    return ""


def serial_from_filename(file_name) -> Optional[str]:
    """Best-effort Delivery Note serial taken from the download file name.

    Most delivery notes carry their number in the file name (e.g.
    "RS-DC-26-27-6.pdf" -> "6", "Delivery Note 55.pdf" -> "55"), so this is a
    useful fallback when OCR cannot read the "Delivery Note No." field.
    Conservative: financial-year pairs ("26-27") and 4-digit years are removed
    first so they never win, and only a word-bounded 1-4 digit token (usually
    the last one) is used. Returns None when nothing plausible is found.
    """
    try:
        stem = Path(str(file_name)).stem
    except Exception:
        return None
    # Drop FY pairs like "26-27" and 4-digit years so "27"/"2026" can't win.
    cleaned = re.sub(r"\b\d{2}-\d{2}\b", " ", stem)
    cleaned = re.sub(r"\b(?:19|20)\d{2}\b", " ", cleaned)
    nums = re.findall(r"\b\d{1,4}\b", cleaned)
    return nums[-1] if nums else None


def _looks_like_reference(value: str) -> bool:
    """True when *value* looks like a reference/order number, not a site name.

    The Site row must come ONLY from "Other References", but the vision model
    sometimes answers it with a "Reference No."-style value instead (or
    substitutes one when "Other References" is missing). Sites are names
    ("Kalpataru Vivant (T-A)"); reference numbers are codes ("REF-12345",
    "RS/DC/26-27/6", "2026-27-0144", "Ref No. 1234"). Rejecting those makes
    the popup leave Site empty so the user adds the real site themselves.
    """
    v = value.strip()
    if not v:
        return True
    # Starts with a reference-type label: "Ref No. 123", "REFERENCE : ...",
    # "SR. NO.", "No. 123", ...
    if re.match(
        r"(?i)^\s*(?:ref(?:erence)?|sr|s\.?\s*no\.?|no\.?)\s*(?:no\.?)?\s*[:#.\-]",
        v,
    ):
        return True
    # Compact code with no spaces that contains a digit: "DN-4521",
    # "RS/DC/26-27/6", "2026-27-0144", "PO-123", "12345".
    if " " not in v and re.search(r"\d", v):
        return True
    return False


def parse_table_response(content: str) -> Dict[str, Optional[str]]:
    """Extract Company/Client/Site/Serial from the model's markdown table.

    Tolerates code fences, extra surrounding text, different label casing and
    values wrapped in ``**``. Fields the model couldn't determine (or that
    came back as "N/A") become ``None``.
    """
    result: Dict[str, Optional[str]] = {
        "company": None, "client": None, "site": None, "serial": None,
    }
    if not content:
        return result

    lines = content.splitlines()
    for raw_line in lines:
        # A table row looks like: | Company (Supplier) | Ruby Steel | .
        # Normalise markdown emphasis first so **Company (Supplier)** and
        # `Company (Supplier)` labels also match.
        line = raw_line.replace("**", "").strip()
        if "|" not in line:
            continue
        for key, label_re in _ROW_PATTERNS.items():
            if result[key] is not None:
                continue  # first row wins
            m = re.search(r"\|\s*" + label_re.pattern + r"\s*\|\s*([^|\n]+?)\s*\|", line, re.IGNORECASE)
            if not m:
                continue
            value = m.group(1).strip().strip("`").strip()
            value = re.sub(r"^\*\*|\*\*$", "", value).strip()
            if value.lower() in _NULL_VALUES:
                value = ""
            if value:
                if key == "serial":
                    value = _clean_serial(value)
                elif key == "site":
                    # Site is "Other References"-only; anything that looks like
                    # a reference number instead is treated as absent so the
                    # user fills the real site in themselves.
                    if _looks_like_reference(value):
                        value = ""
                if value:
                    result[key] = value
    return result


def extract_delivery_note(
    file_path,
    token: str,
    model: str = OCR_MODEL,
    api_base: str = OCR_API_BASE,
    prompt: str = OCR_PROMPT,
    max_tokens: int = OCR_MAX_TOKENS,
    timeout: float = OCR_TIMEOUT,
    known_sites: Optional[List[str]] = None,
) -> Optional[Dict[str, Optional[str]]]:
    """Run OCR on *file_path* and return {company, client, site} (None on failure).

    When ``known_sites`` is given (names already in the config), the prompt is
    rebuilt with them so the model resolves near-same site spellings to the
    existing names. Never raises: network/render/model errors are logged and
    return None so the popup can simply skip auto-fill.
    """
    if known_sites is not None:
        prompt = build_ocr_prompt(known_sites)
    data_url = render_to_data_url(Path(file_path))
    if data_url is None:
        return None

    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": max_tokens,
    }).encode("utf-8")

    endpoint = api_base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        print(f"[ocr] OpenCode Go API error ({e.code}): {detail}")
        return None
    except Exception as exc:
        print(f"[ocr] OpenCode Go API call failed: {exc}")
        return None

    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        print(f"[ocr] unexpected API response: {str(data)[:300]}")
        return None

    result = parse_table_response(content)
    if not any(result.values()):
        usage = data.get("usage") or {}
        fin = (data.get("choices") or [{}])[0].get("finish_reason")
        reason = f"finish_reason={fin}" if fin else "no usage"
        if usage.get("completion_tokens") and usage.get("completion_tokens_details", {}).get("reasoning_tokens"):
            reason = f"reasoning consumed all {usage.get('completion_tokens')} tokens"
        print(f"[ocr] model returned no fields ({reason})")
    return result


class OcrPool:
    """Bounded background OCR worker pool.

    Every file that lands in the watch folder is submitted eagerly, so by the
    time its popup opens the result is usually already cached. At most
    :data:`MAX_CONCURRENT_OCR` vision calls run at once — when many files
    arrive together they are processed in batches of that size: as soon as
    one call finishes, the next queued file starts (never more than 5
    requests in flight).

    Results are cached by resolved path; callers either poll :meth:`get` or
    register a completion callback with :meth:`submit`. Never raises: every
    failure surfaces as a ``None`` result. Workers are daemon threads so the
    app can always quit immediately, even mid-call.
    """

    # Sentinel pushed on shutdown to stop the workers.
    _STOP = object()

    def __init__(
        self,
        token: Optional[str],
        model: str = OCR_MODEL,
        api_base: str = OCR_API_BASE,
        max_concurrent: int = MAX_CONCURRENT_OCR,
        known_sites_provider: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self._token = token
        self._model = model
        self._api_base = api_base
        self._max = max(1, max_concurrent)
        # Called per file (just before the vision call) to fetch the current
        # site catalog, so sites added mid-batch are known to later reads.
        self._known_sites_provider = known_sites_provider
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._results: Dict[str, Optional[Dict[str, Optional[str]]]] = {}
        self._active: set = set()          # paths queued or running
        self._waiters: Dict[str, List[Callable]] = {}
        self._workers = [
            threading.Thread(target=self._worker, daemon=True,
                             name=f"filepicker-ocr-{i}")
            for i in range(self._max)
        ]
        for w in self._workers:
            w.start()

    @property
    def available(self) -> bool:
        """True when an API key is present so OCR can actually run."""
        return bool(self._token)

    def _key(self, file_path) -> str:
        return str(Path(file_path).resolve())

    def get(self, file_path) -> Optional[Dict[str, Optional[str]]]:
        """The cached OCR result for *file_path* (None if not finished yet)."""
        with self._lock:
            return self._results.get(self._key(file_path))

    def submit(self, file_path, on_done: Optional[Callable] = None) -> bool:
        """Queue OCR for *file_path* (no-op when already queued or finished).

        If the result is already cached, *on_done* fires immediately on the
        calling thread. Otherwise it fires (once, from a worker thread) when
        the OCR call completes. Returns True when the file was newly queued.
        """
        if not self.available:
            if on_done is not None:
                try:
                    on_done(None)
                except Exception:
                    pass
            return False
        key = self._key(file_path)
        with self._lock:
            if key in self._results:
                done = True
                result = self._results[key]
            elif key in self._active:
                done = False
                result = None
                if on_done is not None:
                    self._waiters.setdefault(key, []).append(on_done)
                return False
            else:
                self._active.add(key)
                if on_done is not None:
                    self._waiters.setdefault(key, []).append(on_done)
                self._queue.put((Path(file_path), key))
                return True
        if done and on_done is not None:
            try:
                on_done(result)
            except Exception:
                pass
        return False

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                self._queue.task_done()
                return
            path, key = item
            try:
                self._work(path, key)
            except Exception as exc:
                print(f"[ocr] worker error for {path}: {exc}")
            finally:
                self._queue.task_done()

    def _work(self, file_path: Path, key: str) -> None:
        known_sites = None
        if self._known_sites_provider is not None:
            try:
                known_sites = self._known_sites_provider()
            except Exception as exc:
                print(f"[ocr] known-sites fetch error: {exc}")
                known_sites = None
        try:
            result = extract_delivery_note(
                file_path, token=self._token, model=self._model,
                api_base=self._api_base, known_sites=known_sites,
            )
        except Exception as exc:  # belt & braces: extract never raises
            print(f"[ocr] OCR error for {file_path}: {exc}")
            result = None
        with self._lock:
            self._results[key] = result
            self._active.discard(key)
            waiters = list(self._waiters.pop(key, []))
        for cb in waiters:
            try:
                cb(result)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop the workers (in-flight calls finish; queued ones are dropped)."""
        for _ in range(self._max):
            try:
                self._queue.put(self._STOP)
            except Exception:
                pass


if __name__ == "__main__":
    # CLI for testing:  python ocr.py <delivery-note.pdf|image> [model] [api_base]
    if len(sys.argv) < 2:
        print("usage: python ocr.py <file.pdf|file.png> [model] [api_base]")
        sys.exit(2)
    from config import _read_opencode_token

    token = _read_opencode_token()
    if not token:
        print("No OpenCode Go API key found (env FILEPICKER_OPENCODE_TOKEN / OPENCODE_API_KEY / opencode_token.txt).")
        sys.exit(1)
    model = sys.argv[2] if len(sys.argv) > 2 else OCR_MODEL
    api_base = sys.argv[3] if len(sys.argv) > 3 else OCR_API_BASE
    out = extract_delivery_note(sys.argv[1], token=token, model=model, api_base=api_base)
    if out is None:
        print("OCR failed (see logs above).")
        sys.exit(1)
    for key in ("company", "client", "site", "serial"):
        print(f"{key}: {out.get(key)}")