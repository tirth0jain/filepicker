"""OCR of delivery-note documents via the OpenCode Go vision model.

When ``enable_ocr`` is on, every new download (PDF or image) is sent to the
**DeepSeek V4.1 Flash** model on the OpenCode Go catalog
(`opencode.ai/zen/go/v1`, OpenAI-compatible API) with a prompt that asks for:

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
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from version import VERSION
# Financial-year normalisation is shared with the filename builder, so
# the value read off the document and the value written into the file
# name can never disagree about what "25-26" means.
from filename import fy_from_text as _fy_from_text

# --- Providers ------------------------------------------------------------
#
# OCR talks OpenAI-compatible chat completions either way, so one client serves
# both: only the base URL, the path, the auth chain and the model ids differ.
#
#   commandcode  api.commandcode.ai/provider/v1 — the documented Provider API
#                route. The SAME model is also reachable through the CLI
#                transport (/alpha/generate), and that route is a trap for this
#                job: it injects the Command Code CLI's own ~7 400-token
#                scaffolding into every call (measured 2026-11-04), so even a
#                one-line prompt prefilled slowly (4.6-22 s against 2.8-3.0 s
#                on the Provider route). Measured through the Provider API with
#                a real delivery note: 3 325 prompt tokens (2 378 of them
#                image), first token at 3.25 s, 110 output tokens in 0.94 s
#                => 117 tok/s decode, 4.19 s end to end. The model matters more
#                than anything else: DeepSeek V4.1 Flash accepts
#                reasoning_effort "off" and reads the same note in 3.9 s
#                (145 tok/s, 0 reasoning tokens), while GLM-5.3 Flash cannot
#                stop thinking and takes 37.8 s.
#   opencode     opencode.ai/zen/go/v1 — the previous default.
OCR_OPENCODE = "opencode"
OCR_COMMANDCODE = "commandcode"

OCR_PROVIDERS: Dict[str, Dict[str, Any]] = {
    OCR_COMMANDCODE: {
        "label": "CommandCode",
        "api_base": "https://api.commandcode.ai",
        "chat_path": "/provider/v1/chat/completions",
        # Measured on a real delivery note: DeepSeek V4.1 Flash with
        # reasoning_effort "off" reads it in 3.9 s (111 tokens, 145 tok/s,
        # 0 reasoning tokens); GLM-5.3 Flash takes 37.8 s because it cannot
        # stop thinking (the API refuses "off" for it). Speed wins by default;
        # GLM is one tray click away.
        "default_model": "deepseek/deepseek-v4.1-flash",
        # OpenCode Go needs a per-run session id for routing/prompt caching;
        # CommandCode has no such requirement.
        "session_header": False,
        "token_env": ("FILEPICKER_COMMANDCODE_TOKEN", "COMMANDCODE_API_KEY"),
        "token_file": "commandcode_token.txt",
        "models": [
            # GLM cannot turn thinking OFF at all (the Provider API rejects
            # reasoning_effort "off" with "only models that can turn thinking
            # off accept it"), so "low" is its floor. The DeepSeek models on
            # this gateway DO take "off" — and it works: measured 1 completion
            # token, 0 reasoning tokens, 2.1 s. That makes "off" the fastest
            # read on CommandCode, and "none" is not even a valid value here
            # (the API 400s with the accepted set: off|low|medium|high|xhigh|max).
            {"id": "z-ai/glm-5.3-flash", "label": "GLM-5.3 Flash",
             "vision": True,
             "efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "deepseek/deepseek-v4.1-flash",
             "label": "DeepSeek V4.1 Flash", "vision": True,
             "efforts": ["off", "low", "medium", "high", "xhigh", "max"]},
            {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro",
             "vision": True,
             "efforts": ["off", "low", "medium", "high", "xhigh", "max"]},
            {"id": "deepseek/deepseek-v4-flash-vision-exp",
             "label": "DeepSeek V4 Flash Vision (exp)", "vision": True,
             "efforts": ["off", "low", "medium", "high", "xhigh", "max"]},
        ],
    },
    OCR_OPENCODE: {
        "label": "OpenCode Go",
        "api_base": "https://opencode.ai/zen/go/v1",
        "chat_path": "/chat/completions",
        "default_model": "deepseek-v4.1-flash",
        "session_header": True,
        "token_env": ("FILEPICKER_OPENCODE_TOKEN", "OPENCODE_API_KEY"),
        "token_file": "opencode_token.txt",
        "models": [
            {"id": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash",
             "vision": True, "efforts": ["none", "low", "high", "max"]},
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash",
             "vision": True, "efforts": ["none", "low", "high", "max"]},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro",
             "vision": True, "efforts": ["none", "low", "high", "max"]},
        ],
    },
}

# New installs read through CommandCode; a config that pins a model or a base
# decides for itself (see config.ConfigManager.ocr_provider).
OCR_DEFAULT_PROVIDER = OCR_COMMANDCODE
OCR_API_BASE = OCR_PROVIDERS[OCR_DEFAULT_PROVIDER]["api_base"]
OCR_MODEL = OCR_PROVIDERS[OCR_DEFAULT_PROVIDER]["default_model"]


def provider_spec(provider: Optional[str] = None) -> Dict[str, Any]:
    """The spec for *provider*, falling back to the default provider."""
    name = str(provider or "").strip().lower()
    return OCR_PROVIDERS.get(name) or OCR_PROVIDERS[OCR_DEFAULT_PROVIDER]


def provider_for_base(api_base: Optional[str]) -> Optional[str]:
    """Which provider serves *api_base*, when it is one we know."""
    base = str(api_base or "").strip().rstrip("/").lower()
    if not base:
        return None
    for name, spec in OCR_PROVIDERS.items():
        if spec["api_base"].rstrip("/").lower() == base:
            return name
    return None


def provider_for_model(model: Optional[str]) -> Optional[str]:
    """Which provider serves *model*, when it is one we know."""
    wanted = str(model or "").strip()
    for name, spec in OCR_PROVIDERS.items():
        if any(entry["id"] == wanted for entry in spec["models"]):
            return name
    return None


def model_spec(model: Optional[str],
               provider: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The catalog entry for *model* — *provider* first, then all providers."""
    wanted = str(model or "").strip()
    if not wanted:
        return None
    order = [str(provider).lower()] if provider else []
    order += [p for p in OCR_PROVIDERS if p not in order]
    for name in order:
        for entry in OCR_PROVIDERS.get(name, {}).get("models", []):
            if entry["id"] == wanted:
                return entry
    return None


def ocr_models(provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """The models the picker offers for *provider* (every provider if None)."""
    if provider:
        return list(provider_spec(provider)["models"])
    out: List[Dict[str, Any]] = []
    for spec in OCR_PROVIDERS.values():
        out.extend(spec["models"])
    return out

# Model ids that OLDER builds wrote into config.json as the then-current
# default. config.json is the source of truth for the model id (it is a
# local-only key — never synced from GitHub nor pushed), so merely changing
# the default above would never reach an existing install: the file keeps
# pinning the old name. config.py upgrades a stored value from this list to
# OCR_MODEL when it loads (and persists it on the next save). Any OTHER value
# is a deliberate per-machine override and is left untouched.
LEGACY_OCR_MODELS = ("deepseek-v4-flash-vision-exp",)

# The model burns ~1500 tokens reasoning on a simple delivery note, and
# harder documents (long tables, faint scans, the full known-sites/clients
# lists in the prompt) have consumed the whole old 4096-token budget on
# reasoning alone, leaving nothing for the answer table. 8192 gave it room,
# but on dense documents the reasoning still consumed the ENTIRE budget and
# the model returned no fields at all ("reasoning consumed all 8192
# tokens") — doubled to 16384 so the answer table always has room. The budget
# is only a ceiling (it costs nothing when unused) and is kept at 16384 even
# though OCR_THINKING now turns the thinking off: a gateway that refuses every
# thinking field (see _THINKING_REJECTED) falls back to the model's own
# default, which needs the full budget again — and so does the one re-read
# that escalates to graded thinking.
OCR_MAX_TOKENS = 16384

# --- How hard the model thinks before answering ---------------------------
#
# THE latency lever for a read. DeepSeek's API enables THINKING MODE BY
# DEFAULT with effort "high" (api-docs.deepseek.com/guides/thinking_mode) and
# `reasoning_effort` only *grades* that thinking down — "low"/"high"/"max" are
# the supported levels ("medium" maps back up to "high") — it never stops it.
# A delivery note needs no chain of thought to copy five fields out of, so a
# read that thinks spends thousands of tokens (tens of seconds, varying with
# the document and the gateway's load) before writing a five-row table. That
# is where "OCR sometimes takes 30s+ for one file" came from, and why 0.6.33's
# `reasoning_effort="low"` did not fix it.
#
# The read therefore asks for thinking to be OFF and only degrades when the
# gateway refuses, walking this ladder (see _thinking_plans):
#
#   1. reasoning_effort "none"        — verified live against the OpenCode Go
#                                       gateway: the reply carries no
#                                       reasoning_content and completion_tokens
#                                       drop to single digits (opencode#27555)
#   2. thinking {"type": "disabled"}  — DeepSeek's documented OpenAI-format
#                                       toggle for the same thing
#   3. reasoning_effort "low"         — graded thinking (the 0.6.33 setting)
#   4. nothing at all                 — the model's own default (high)
#
# A step is abandoned for the rest of the run when the gateway REJECTS it
# (400/404/422) *or* when it is accepted but the reply still carries reasoning
# tokens — a 200 does not prove the field was honoured, and trusting that 200
# is exactly how the 0.6.33 fix silently did nothing. See
# _reject_plan.
OCR_THINKING = "off"
OCR_THINKING_LEVELS = ("off", "low", "high", "max", "default")
# The reasoning_effort value that means "do not think at all".
OCR_THINKING_OFF_EFFORT = "none"
# Reasoning tokens in a reply that asked for thinking OFF prove the request
# was ignored (a thinking-free read has none at all).
OCR_THINKING_VERIFY_TOKENS = 16
# A thinking-free read that cannot produce the table means the document really
# needs reasoning: read it once more with graded thinking instead of returning
# nothing (accuracy is never traded away for speed).
OCR_THINKING_ESCALATION_EFFORT = "low"

# Thinking level used by the popup's "↻ Retry OCR" button. A retry is a
# deliberate "look again, more carefully" — the user pressed it because the
# first answer was wrong — so it is NOT the same request again: an automatic
# read stays thinking-free (fast), while a retry thinks. With `ocr_thinking`
# already set to a graded level the retry keeps that level instead (see
# retry_thinking_level).
OCR_RETRY_THINKING = "low"

# Legacy knob (config.json "ocr_reasoning_effort", local-only): kept so old
# configs and callers still resolve. The default now maps to thinking OFF —
# see config.ConfigManager.ocr_thinking.
OCR_REASONING_EFFORT = "low"

# Statuses that specifically mean "this gateway does not know that field".
# They only change the WORDING of the log: any failure of a rung that carries
# a thinking field drops that rung and tries the next one, because the ladder
# always ends with the plain request the app sent before thinking control
# existed — so no gateway behaviour around these fields can break OCR.
OCR_EFFORT_REJECT_STATUS = {400, 404, 422}

# Total wall-clock budget for ONE read (seconds), covering every attempt and
# every retry backoff. A read that cannot finish inside it reports a timeout
# instead of holding the popup hostage.
OCR_TOTAL_BUDGET = 100.0

# Socket timeout for a single attempt (seconds). Deliberately well below the
# total budget: a stalled connection must not eat the whole read.
OCR_TIMEOUT = 45

# An attempt that already took this long is not retried: a slow failure is
# rarely a transient one, and retrying it multiplies the wait the user sees.
OCR_SLOW_ATTEMPT_RETRY_S = 20.0

# Maximum image dimension sent to the model (pixels). Keeps the request
# small and fast without hurting text legibility.
OCR_MAX_IMAGE_DIM = 1600

# A PDF page is never rendered larger than this multiple of its natural size
# (see _pdf_zoom): rendering straight at the size the model gets beats
# rendering at 2x and shrinking the result, and it bounds the work a
# pathological page can cause.
OCR_MAX_PDF_ZOOM = 2.0

# JPEG quality used when compressing the rendered page for the API call. 85
# is visually indistinguishable from 90 on a scanned page but ~13% fewer
# bytes, i.e. less to upload before the model can even start.
OCR_JPEG_QUALITY = 85

# Maximum number of vision calls in flight at the same time. Every file that
# lands in the watch folder is submitted for OCR IMMEDIATELY (see main.py) and
# the pool spawns one worker per queued file up to this ceiling, so a batch of
# downloads is read SIMULTANEOUSLY instead of in waves. The ceiling only
# exists so that dropping hundreds of files at once cannot open hundreds of
# sockets at once; anything above it starts as the first reads finish.
#
# Kept deliberately modest: 32 simultaneous vision calls on one subscription
# is a rate-limit magnet (429 -> retry -> exactly the "sometimes 30s+" the
# user saw), and it made one file's read compete with 31 others for gateway
# capacity. With thinking off a read is ~3s, so 8 at a time still clears a
# batch of 24 in about 9s while leaving the gateway room to answer each call
# at full speed.
MAX_CONCURRENT_OCR = 8

# Cloudflare in front of the OpenCode gateway blocks the default
# "Python-urllib" user agent (HTTP 403, error code 1010), so every request
# carries a browser-like application UA.
_UA = f"FilePicker/{VERSION} (Windows; DeliveryNote OCR)"

# HTTP statuses that deserve an automatic retry: gateway-side errors and
# rate limits (the user saw "OpenCode Go API error (500) ... Internal server
# error" — a transient gateway failure that usually succeeds on retry).
# Everything else (400/401/403/404/...) is permanent: fail immediately.
OCR_RETRY_STATUS = {429, 500, 502, 503, 504}
# Extra attempts after the first call: 1 initial + 2 retries = 3 maximum.
OCR_RETRY_ATTEMPTS = 2
# Seconds to wait before each retry (short backoff: 2s, then 5s).
OCR_RETRY_BACKOFF = (2.0, 5.0)
# A 429 may carry Retry-After; waiting that long is only worth it up to this
# many seconds, so "come back in an hour" cannot stall a popup.
OCR_MAX_RETRY_WAIT = 15.0

# OpenCode Go (see https://opencode.ai/docs/go/) now requires every request
# to carry a stable conversation/session ID in `x-opencode-session` —
# without it the gateway answers 400 "MissingSessionID" and the request
# cannot be routed/cached efficiently. One ID per app run is exactly right
# for FilePicker: all OCR reads share the same prompt text, so a stable ID
# lets the gateway reuse prompt caches across the whole batch.
OCR_SESSION_ID = str(uuid.uuid4())

# Thinking plans this run has ruled out — because the gateway rejected the
# field (400/404/422) or because a reply proved it was ignored (it still
# carried reasoning tokens). Remembered for the whole run so only the FIRST
# file pays for the discovery and every later read goes straight to the rung
# that works. See _thinking_plans / _pick_plan.
_THINKING_REJECTED: set = set()
_THINKING_PLAN_LOCK = threading.Lock()


def _plan_key(plan: Dict[str, Any]) -> str:
    """A short, loggable name for one thinking plan."""
    if not plan:
        return "model default"
    if "reasoning_effort" in plan:
        return f"reasoning_effort={plan['reasoning_effort']}"
    if "thinking" in plan:
        return f"thinking.type={plan['thinking'].get('type')}"
    return "?"


def _thinking_plans(level: Optional[str] = None,
                    model: Optional[str] = None) -> List[Dict[str, Any]]:
    """The ordered thinking plans to try for *level*, fastest first.

    The ladder always ends with "send nothing", so a gateway that knows none
    of these fields still reads the document (the model then uses its own
    default). *level* is one of :data:`OCR_THINKING_LEVELS`.

    *model* makes the ladder model-aware, which matters because "off" is not
    portable: DeepSeek answers `reasoning_effort: "none"`, while GLM 5.3 Flash
    only accepts low/high/max, so asking it to stop thinking means asking for
    "low". Sending "none" to a model that does not define it is at best ignored
    and at worst a 400, and either way it costs a round trip to learn.
    """
    level = str(level if level is not None else OCR_THINKING).strip().lower()
    if level not in OCR_THINKING_LEVELS:
        level = OCR_THINKING
    entry = model_spec(model) if model else None
    efforts = list((entry or {}).get("efforts")
                   or ["none", "low", "high", "max"])
    plans: List[Dict[str, Any]] = []

    def _add(plan: Dict[str, Any]) -> None:
        if plan not in plans:
            plans.append(plan)

    if level == "default":
        return [{}]
    if level == "off":
        if "off" in efforts:
            # CommandCode's own spelling, and the only one that truly stops
            # the thinking there (DeepSeek: 1 token, 0 reasoning, 2.1 s).
            _add({"reasoning_effort": "off"})
        elif "none" in efforts:
            # OpenCode Go's spelling for the same thing.
            _add({"reasoning_effort": OCR_THINKING_OFF_EFFORT})
            _add({"thinking": {"type": "disabled"}})
        # Next cheapest: the lowest graded level this model defines. For GLM
        # that IS "off" (it defines low/medium/high/xhigh/max and rejects
        # "off" outright); for DeepSeek it is the fallback if "off" is refused.
        cheapest = "low" if "low" in efforts else efforts[0]
        _add({"reasoning_effort": cheapest})
    else:
        _add({"reasoning_effort": level})
    # The plain request last: the model's own default always works.
    _add({})
    return plans


def _pick_plan(plans: List[Dict[str, Any]]) -> int:
    """Index of the first plan in *plans* this run has not ruled out."""
    with _THINKING_PLAN_LOCK:
        rejected = set(_THINKING_REJECTED)
    for index, plan in enumerate(plans):
        if _plan_key(plan) not in rejected:
            return index
    return len(plans) - 1


def _reject_plan(plan: Dict[str, Any], reason: str) -> None:
    """Rule a plan out for the rest of the run (idempotent, never raises).

    The empty plan is never rejected — it is the last rung and carries no
    field that could be wrong.
    """
    if not plan:
        return
    key = _plan_key(plan)
    with _THINKING_PLAN_LOCK:
        if key in _THINKING_REJECTED:
            return
        _THINKING_REJECTED.add(key)
    print(f"[ocr] not sending {key} again this run: {reason}")


def _reset_thinking_plans() -> None:
    """Forget every ruled-out plan (a fresh run; used by tests)."""
    with _THINKING_PLAN_LOCK:
        _THINKING_REJECTED.clear()


def _reasoning_tokens(data: dict) -> int:
    """How many reasoning tokens a reply spent (0 when it did not think).

    The usage block is the only trustworthy evidence that a thinking control
    was honoured: a gateway can answer 200 while quietly ignoring the field,
    which is precisely how a "low effort" request can still think for 30s.
    """
    usage = (data or {}).get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if not isinstance(tokens, int) or tokens <= 0:
        # Some gateways report it flatter, at the top level of `usage`.
        flat = usage.get("reasoning_tokens")
        tokens = flat if isinstance(flat, int) else 0
    if isinstance(tokens, int) and tokens > 0:
        return tokens
    # Some gateways only expose the chain of thought itself.
    try:
        message = (data.get("choices") or [{}])[0].get("message") or {}
        if str(message.get("reasoning_content") or "").strip():
            return max(OCR_THINKING_VERIFY_TOKENS, 1)
    except (AttributeError, IndexError, TypeError):
        pass
    return 0

# The extraction prompt — verbatim from the feature spec (Serial Number added
# in 0.6.4: read from the "Delivery Note No." field, digits only, 1-4 digits.
# Site made strictly "Other References"-only in 0.6.6: the model must never
# substitute a "Reference No." / "Ref No." value for the missing site.)
#
# 0.6.39: the catalog is NOT sent to the model any more. Until 0.6.38 the
# prompt carried every known site (~106 names, near-duplicates across clients)
# and client (~199) and ordered the model to "output the Known Site name
# EXACTLY as listed above". That turned reading one printed line into a
# fuzzy-selection over a long list, and a wrong pick from that list is
# indistinguishable from a misread: a document whose "Other References" said
# "Lodha Kharadi T-2" came back as a DIFFERENT real site, "Lodha Sital Baug"
# (a name the old prompt even used as its example). The app's own matcher
# already resolves the document's spelling to the catalog (case, spacing,
# articles, one-letter variants, one extra word, trailing tower/wing/phase
# designators — see config.find_near_name), so the list bought nothing and
# risked exactly this. The model now copies what is printed; the app maps it.
OCR_PROMPT = """You are given a delivery note document. Extract the following information and present it in a table format:

1. Company (Supplier) - the company supplying the goods (e.g., Ruby Steel)
2. Client (Buyer) - the company being supplied to, as printed in the "Buyer (Bill to)" or "Consignee (Ship to)" block
3. Site - ONLY the value of the field literally labelled "Other References" (usually a project or site name, sometimes with a tower/wing/phase written with it)
4. Serial Number - the number in the "Delivery Note No." field (e.g., "RS/DC/26-27/6" -> 6, "RS/DC/26-27/55" -> 55)
5. Financial Year - the two-digit year pair in the SAME "Delivery Note No." value (e.g., "RS/DC/25-26/123" -> 25-26, "RS/DC/26-27/6" -> 26-27). Two digits, a hyphen, two digits — never a full year
6. Description of Goods - ONLY the BOLD heading words of each item in the goods/items table (the short material name printed in bold at the head of the row) — NOT the smaller normal-weight description lines written below each heading

Rules:
- COPY WHAT IS PRINTED. Every value is the text printed on THIS document: the same words, the same spelling, the same tower/wing/phase suffix. Never correct a spelling, translate, expand an abbreviation, tidy up a name, or replace a name with a similar one you have seen on another document or know from elsewhere. A value that looks wrong is still copied as printed — the app matches names against its own catalog itself.
- Company is the supplier (from the "From" / "RUBY STEEL" section)
- Client is the buyer/consignee (from "Buyer (Bill to)" or "Consignee (Ship to)" section)
- Site MUST come ONLY from the field literally labelled "Other References". NEVER use "Reference No.", "Ref No.", "SR. No.", "Bill No.", "Invoice No.", "Delivery Note No.", "PO No." or any other field for Site
- Site is that field's WHOLE value, exactly as printed, including any tower/wing/unit part written with it: if it reads "X T-2", copy "X T-2" — never shorten it to "X", and never replace X with the name of another site. The same for a spelled-out designator ("X Tower 2" stays "X Tower 2")
- If the document has no "Other References" field, leave the Site cell EMPTY (do not substitute any other value)
- Serial Number is the numeric part of the "Delivery Note No." value: digits only, 1-4 digits, usually the part after the last "/" (e.g. "RS/DC/26-27/6" -> 6, "RS/DC/26-27/55" -> 55)
- Financial Year is the year pair inside the SAME "Delivery Note No." value, copied as two two-digit years joined by a hyphen ("25-26", "26-27"). It is NOT the current year and NOT a guess: if the value has no year pair, leave Financial Year empty
- If the Delivery Note No. is not present, leave Serial Number AND Financial Year empty
- Description of Goods: transcribe ONLY the BOLD heading of each item row (the short material name printed in bold). IGNORE the smaller normal-weight description lines written BELOW each heading. If no text in the table is bold, transcribe only the FIRST line of each item (the heading), never the sub-lines below. Do NOT invent, translate, correct or summarise item names. If there is no goods table/column, leave it EMPTY
- Capitalisation may be normalised to Title Case (Description of Goods keeps the document's own wording); nothing else about a value may change

Output format:

| Role | Value |
|------|-------|
| Company (Supplier) | [Name] |
| Client (Buyer) | [Name] |
| Site (Other References) | [Name] |
| Serial Number (Delivery Note No.) | [Number] |
| Financial Year (Delivery Note No.) | [YY-YY] |
| Description of Goods | [Bold item headings, comma separated] |"""

# The catalog is deliberately NOT part of the prompt any more (see the note on
# OCR_PROMPT): the model copies the printed value and the app resolves it. The
# near-match rules that used to be spelled out to the model live in
# config.find_near_name / ConfigManager.site_display_name, where they are
# deterministic and testable.

# Labels the model is asked to emit, mapped to our result keys. Matching is
# case-insensitive and tolerant of extra whitespace/backticks around the row.
_ROW_PATTERNS = {
    "company": re.compile(r"Company\s*\(Supplier\)", re.IGNORECASE),
    "client": re.compile(r"Client\s*\(Buyer\)", re.IGNORECASE),
    "site": re.compile(r"Site\s*\(Other\s*References\)", re.IGNORECASE),
    "fy": re.compile(
        # "Financial Year (Delivery Note No.)" / "Financial Year" / "FY"
        r"(?:Financial\s*Year|FY)"
        r"\s*(?:\(\s*(?:Delivery\s*Note|Invoice|Bill)[^)]*\))?",
        re.IGNORECASE,
    ),
    "serial": re.compile(
        # "Serial Number (Delivery Note No.)" / "Serial Number" /
        # "Delivery Note No." / "Delivery Note Number" / "Serial No."
        r"(?:Serial\s*(?:Number|No\.?)|Delivery\s*Note\s*(?:Number|No\.?))"
        r"\s*(?:\(\s*Delivery\s*Note\s*(?:Number|No\.?)\s*\))?",
        re.IGNORECASE,
    ),
    # Verbatim item descriptions from the goods table (used to pre-select the
    # catalog materials the delivery actually contains — see the popup's
    # _goods_material_matches).
    "goods": re.compile(
        r"(?:Description\s+of\s+Goods|Goods\s+Description|Item\s+Description)",
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

    PDFs are rendered with PyMuPDF at the size the model is given (see
    :func:`_pdf_zoom`); images are loaded with Pillow (first frame). Returns
    None for unsupported files or render errors — callers continue without
    OCR.
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


def _pdf_zoom(page_rect, max_dim: int) -> float:
    """Zoom that lands the long side of *page_rect* on *max_dim* pixels.

    Rendering AT the target size — instead of rendering at 2x and shrinking
    the result afterwards — produces the same image while skipping a full PNG
    encode, a PNG decode and a LANCZOS resample. It also makes a pathological
    page cheap: a CAD drawing whose MediaBox is metres wide used to be
    rendered at ~20000px (seconds of work and gigabytes of RAM) and then
    thrown away down to 1600px, where it now costs milliseconds.
    """
    try:
        longest = max(float(page_rect.width), float(page_rect.height))
    except (AttributeError, TypeError, ValueError):
        longest = 0.0
    if not (longest > 0.0) or longest != longest or longest == float("inf"):
        return 1.0
    return min(max_dim / longest, OCR_MAX_PDF_ZOOM)


def _pixmap_to_image(pix):
    """A PIL image from a PyMuPDF pixmap, with no PNG encode/decode detour."""
    from PIL import Image

    mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(getattr(pix, "n", 0))
    if mode is None:
        return None
    try:
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    except Exception:
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
        zoom = _pdf_zoom(page.rect, max_dim)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        image = _pixmap_to_image(pix)
        if image is None:
            # Any surprise in the buffer layout: fall back to the old PNG path
            # rather than losing OCR for this file.
            return _pil_to_data_url(io.BytesIO(pix.tobytes("png")),
                                    max_dim=max_dim, quality=quality)
    return _pil_to_data_url(image, max_dim=max_dim, quality=quality)


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


def fy_from_reference(value) -> Optional[str]:
    """The financial year inside a "Delivery Note No."-style value.

    "RS/DC/25-26/123" -> "25-26" (the year the DOCUMENT belongs to, which is
    what the filename needs — not the current year). Returns None when the
    value holds no consecutive year pair.
    """
    return _fy_from_text(value)


def fy_from_filename(file_name) -> Optional[str]:
    """Best-effort financial year taken from the download file name.

    Most delivery notes carry their "Delivery Note No." in the file name
    ("RS-DC-25-26-7.pdf" -> "25-26"), so this fills the popup's Financial
    Year when the OCR read returned none. Returns None when the name has no
    year pair.
    """
    try:
        stem = Path(str(file_name)).stem
    except Exception:
        return None
    return _fy_from_text(stem)


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
    """Extract Company/Client/Site/Serial/Goods from the model's markdown table.

    Tolerates code fences, extra surrounding text, different label casing and
    values wrapped in ``**``. Fields the model couldn't determine (or that
    came back as "N/A") become ``None``.
    """
    result: Dict[str, Optional[str]] = {
        "company": None, "client": None, "site": None, "serial": None,
        "fy": None, "goods": None,
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
                elif key == "fy":
                    # The model may answer with a full year ("2025-26") or the
                    # whole reference ("RS/DC/25-26/123") — keep only the year
                    # pair, and drop anything that is not one.
                    value = fy_from_reference(value) or ""
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
    api_base: Optional[str] = None,
    prompt: str = OCR_PROMPT,
    max_tokens: int = OCR_MAX_TOKENS,
    timeout: float = OCR_TIMEOUT,
    on_error: Optional[Callable[[str], None]] = None,
    reasoning_effort: Optional[str] = None,
    thinking: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[Dict[str, Optional[str]]]:
    """Run OCR on *file_path* and return {company, client, site, serial, goods}.

    ``goods`` is the BOLD heading words of the "Description of Goods" table
    (comma-separated item names; the sub-description lines below each heading
    are excluded); the popup matches it against the material catalog to
    pre-select the materials the delivery contains.

    The model is asked to COPY the printed values (no catalog is sent to it —
    see :data:`OCR_PROMPT`); resolving a site/client to the app's catalog is
    the popup's job (config.find_near_name), which is deterministic.

    ``thinking`` is how hard the model may think before answering — one of
    :data:`OCR_THINKING_LEVELS`, default :data:`OCR_THINKING` ("off", the
    fastest; see the ladder note above). ``reasoning_effort`` is the legacy
    spelling of the same knob, still accepted from old callers. A gateway that
    rejects a thinking field costs at most one extra round trip: the plan is
    dropped for the rest of the run and the next rung is used. A reply that
    proves the field was ignored (it still carries reasoning tokens) is
    treated the same way, so a "200 OK" can never hide a slow read again.

    Transient gateway failures (HTTP 429/500/502/503/504) are retried with a
    short backoff (:data:`OCR_RETRY_ATTEMPTS` x :data:`OCR_RETRY_BACKOFF`,
    honouring ``Retry-After``); a single attempt is capped at *timeout* and
    the whole read at :data:`OCR_TOTAL_BUDGET`. When the call ultimately fails
    and *on_error* is given, it is called with a one-line message (the same
    text that is logged) — callers use it to tell the user WHY OCR failed.
    Never raises: network/render/model errors are logged and return None so
    the popup can simply skip auto-fill (or offer a retry).
    """
    # Rendering is timed separately: it happens BEFORE the request, so a slow
    # render used to be invisible in the "read in Xs" line (the number the
    # user sees) and made OCR look mysteriously slow.
    render_started = time.monotonic()
    data_url = render_to_data_url(Path(file_path))
    render_s = time.monotonic() - render_started
    if data_url is None:
        return None

    spec = provider_spec(provider)
    endpoint = (api_base or spec["api_base"]).rstrip("/") + spec["chat_path"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Accept": "text/event-stream, application/json",
    }
    if spec.get("session_header"):
        headers["x-opencode-session"] = OCR_SESSION_ID
    level = _thinking_level(thinking if thinking is not None else reasoning_effort)
    plans = _thinking_plans(level, model)
    payload_base = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": max_tokens,
    }

    def _report(msg: str) -> None:
        if on_error is not None:
            try:
                on_error(msg)
            except Exception:
                pass

    started = time.monotonic()
    deadline = started + OCR_TOTAL_BUDGET
    data, plan, attempts = _request_with_thinking(
        endpoint, headers, payload_base, plans, timeout, deadline, _report)
    if data is not None:
        _verify_thinking(plan, data)

    content = _message_content(data) if data is not None else ""
    result = parse_table_response(content) if content else {}

    # Nothing came back although the model answered: a document that needs
    # real reasoning (faint scan, unusual layout) is re-read once WITH graded
    # thinking rather than silently returning nothing. Speed is never bought
    # with accuracy — only the first, fast read is cheap.
    #
    # Only a read that was asked NOT to think is escalated: re-reading with
    # "low" after a read that already used graded thinking (the user set
    # ocr_thinking to low/high/max) would be the same request or a weaker one,
    # so it could only waste a call.
    if (data is not None and not any(result.values()) and content.strip()
            and _asks_for_no_thinking(plan)):
        escalation = [{"reasoning_effort": OCR_THINKING_ESCALATION_EFFORT}, {}]
        index = _pick_plan(escalation)
        print(f"[ocr] {Path(file_path).name}: no fields from the thinking-free "
              f"read — re-reading with {_plan_key(escalation[index])}")
        data2, plan2, attempts2 = _request_with_thinking(
            endpoint, headers, payload_base, escalation, timeout, deadline,
            _report, start_index=index)
        attempts += attempts2
        if data2 is not None:
            data, plan = data2, plan2
            content = _message_content(data)
            result = parse_table_response(content)

    elapsed = time.monotonic() - started
    _log_timing(file_path, elapsed, data, _plan_key(plan), render_s, attempts,
                len(data_url))
    if data is None:
        return None

    if not any(result.values()):
        usage = data.get("usage") or {}
        fin = (data.get("choices") or [{}])[0].get("finish_reason")
        reason = f"finish_reason={fin}" if fin else "no usage"
        if usage.get("completion_tokens"):
            reason = (f"reasoning consumed all {usage.get('completion_tokens')} "
                      f"tokens")
        print(f"[ocr] model returned no fields ({reason})")
    return result


def _thinking_level(value) -> str:
    """Normalise a configured thinking level to :data:`OCR_THINKING_LEVELS`.

    "medium"/"xhigh" are DeepSeek aliases that map back UP to "high", so they
    are normalised here (a "medium" request would otherwise think as hard as
    "high" while looking like a cheap setting). Anything unknown — including
    the empty string — falls back to :data:`OCR_THINKING`.
    """
    level = str(value or "").strip().lower()
    if level in ("medium", "xhigh"):
        level = "high"
    if level in OCR_THINKING_LEVELS:
        return level
    return OCR_THINKING


def retry_thinking_level(configured) -> str:
    """The thinking level for a "↻ Retry OCR" read.

    A retry is the user saying "that answer was wrong, look again" — so it
    must NOT be the same request again (the old button re-ran an identical
    call, which is why a retry so often produced the same wrong fields). With
    automatic reads thinking-free (fast), a retry thinks: :data:`OCR_RETRY_THINKING`.

    A config already asking for graded thinking keeps its own level (there is
    nothing stronger to add), and the plain "default" level is kept too — it
    already lets the model think.
    """
    level = _thinking_level(configured)
    if level in ("off", "low"):
        # "off" is the fast automatic level, so the retry adds the thinking;
        # "low" is already the retry level.
        return OCR_RETRY_THINKING
    return level


def _asks_for_no_thinking(plan: Dict[str, Any]) -> bool:
    """True when *plan* is one of the "do not think" rungs."""
    if not plan:
        return False
    if plan.get("reasoning_effort") == OCR_THINKING_OFF_EFFORT:
        return True
    return (plan.get("thinking") or {}).get("type") == "disabled"


def _verify_thinking(plan: Dict[str, Any], data: dict) -> bool:
    """Check that a "no thinking" plan really produced a thinking-free reply.

    A gateway can answer 200 while quietly ignoring the field, which is how a
    request that asked for a short think still spent 30s reasoning. The usage
    block is the evidence: when it shows reasoning tokens for a rung that
    asked for none, that rung is ruled out for the rest of the run.

    Returns True when the plan was proven ineffective.
    """
    if not _asks_for_no_thinking(plan):
        return False
    tokens = _reasoning_tokens(data)
    if tokens <= OCR_THINKING_VERIFY_TOKENS:
        return False
    _reject_plan(plan, f"the reply still spent {tokens} reasoning tokens, so "
                       f"the gateway ignored it")
    return True


def _retry_delay(error, retries: int) -> float:
    """Seconds to wait before retry *retries*+1.

    A 429's ``Retry-After`` wins when the gateway sends one — but it is capped
    at :data:`OCR_MAX_RETRY_WAIT`, because "come back in an hour" must not
    stall a popup that is waiting for its fields.
    """
    base = OCR_RETRY_BACKOFF[min(retries, len(OCR_RETRY_BACKOFF) - 1)]
    try:
        raw = error.headers.get("Retry-After") or ""
    except Exception:
        raw = ""
    try:
        wait = float(str(raw).strip())
    except (TypeError, ValueError):
        return base
    return max(base, min(wait, OCR_MAX_RETRY_WAIT))


def _message_content(data: dict) -> str:
    """The assistant text of a chat completion ("" when the shape is odd)."""
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


# Set once if a gateway rejects the `stream` fields: the read then falls back to
# the plain buffered request, so streaming can never be the reason a read fails.
_STREAM_DISABLED = False
# Per-thread decode timing of the last streamed read (the pool runs one read per
# worker thread, so the thread-local IS the read's own figure).
_STREAM_STATS = threading.local()


def _read_chat_response(req: urllib.request.Request, timeout: float) -> dict:
    """Read one chat completion, assembling an SSE stream into the usual shape.

    Returns the ordinary ``{"choices": [{"message": {"content": ...}}], ...}``
    dict, so every caller downstream is unchanged. The payload asks for
    ``stream: true`` (see ``_request_with_thinking``); a gateway that ignores the
    flag answers with plain JSON, which this also handles.

    Streaming is what the harness does for the same reason: the first token
    arrives in ~3.2 s and the rest stream at the model's decode rate (measured
    117 tok/s on GLM 5.3 Flash), so the read reports the rate instead of guessing
    it and never waits on a buffered body.
    """
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # A response object is not required to expose headers (a test double,
        # or a transport that wraps urlopen): without them we cannot know it
        # is a stream, so read the body as plain JSON instead of failing.
        headers = getattr(resp, "headers", None)
        try:
            ctype = (headers.get("Content-Type") or "").lower() if headers else ""
        except Exception:
            ctype = ""
        if "event-stream" not in ctype and "ndjson" not in ctype:
            return json.loads(resp.read().decode("utf-8"))
        text = ""
        usage = None
        finish = None
        first_at = None
        last_at = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                event = json.loads(chunk)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            choice = (event.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                if first_at is None:
                    first_at = time.monotonic()
                text += piece
                last_at = time.monotonic()
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        _STREAM_STATS.decode_s = ((last_at - first_at)
                                  if first_at and last_at else 0.0)
        data: Dict[str, Any] = {
            "choices": [{"message": {"content": text},
                         "finish_reason": finish}],
        }
        if usage:
            data["usage"] = usage
        return data


def _request_with_thinking(
    endpoint: str,
    headers: Dict[str, str],
    payload_base: dict,
    plans: List[Dict[str, Any]],
    timeout: float,
    deadline: float,
    report: Callable[[str], None],
    start_index: Optional[int] = None,
) -> tuple:
    """POST one read, walking the thinking ladder and retrying gateway errors.

    Returns ``(data, plan, attempts)``: the parsed reply (None when every rung
    and every retry failed), the plan that produced it, and one
    ``(seconds, status)`` entry per HTTP attempt for the timing line.
    """
    attempts: List[tuple] = []
    retries = 0
    index = _pick_plan(plans) if start_index is None else start_index
    while True:
        plan = plans[index]
        payload = dict(payload_base)
        payload.update(plan)
        if not _STREAM_DISABLED:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, headers=headers, method="POST")

        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            msg = f"OCR gave up after {OCR_TOTAL_BUDGET:.0f}s (budget spent)"
            print(f"[ocr] {msg}")
            report(msg)
            return None, plan, attempts

        attempt_started = time.monotonic()
        try:
            data = _read_chat_response(req, timeout=min(timeout, remaining))
            attempts.append((time.monotonic() - attempt_started, "ok"))
            return data, plan, attempts
        except urllib.error.HTTPError as e:
            took = time.monotonic() - attempt_started
            attempts.append((took, f"HTTP {e.code}"))
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:300]
            except Exception:
                pass
            # A gateway that does not know the `stream` fields rejects the whole
            # body. Turn streaming off for the rest of the run and retry the same
            # rung, so the fast path can never be the reason a read fails.
            if (e.code == 400 and not _STREAM_DISABLED
                    and "stream" in detail.lower()):
                globals()["_STREAM_DISABLED"] = True
                print("[ocr] gateway rejected the stream fields — "
                      "retrying with a buffered request")
                continue
            # ANY failure of a rung that carries a thinking field is treated as
            # "that field may be the problem": it is ruled out for the whole
            # run and the next rung is tried straight away, with no backoff.
            # This is what makes the promise unconditional — the ladder ends
            # with exactly the plain request the app sent before thinking
            # control existed, so no gateway behaviour around these fields can
            # ever break OCR. Retries are spent only on the LAST rung, where
            # there is no alternative shape left to try, so a genuinely-down
            # gateway is not hammered four times over.
            if plan and index + 1 < len(plans):
                why = ("the gateway does not know the field"
                       if e.code in OCR_EFFORT_REJECT_STATUS
                       else "the gateway errored on it")
                _reject_plan(plan, f"{why} ({e.code})"
                                   + (f": {detail[:120]}" if detail else ""))
                index += 1
                continue
            msg = f"OpenCode Go API error ({e.code})"
            if detail:
                msg += f": {detail}"
            # Gateway hiccup / rate limit: wait briefly and try again — but
            # only when the failed attempt was quick and the read still has
            # budget left, so retries can never turn a 10s read into a 40s one.
            if e.code in OCR_RETRY_STATUS and retries < OCR_RETRY_ATTEMPTS:
                delay = _retry_delay(e, retries)
                if took >= OCR_SLOW_ATTEMPT_RETRY_S:
                    print(f"[ocr] {msg} — not retrying: the attempt already "
                          f"took {took:.0f}s")
                elif time.monotonic() + delay + 2.0 > deadline:
                    print(f"[ocr] {msg} — not retrying: only "
                          f"{max(0.0, deadline - time.monotonic()):.0f}s of the "
                          f"read budget is left")
                else:
                    retries += 1
                    print(f"[ocr] {msg} — retry {retries}/{OCR_RETRY_ATTEMPTS} "
                          f"in {delay:.0f}s")
                    time.sleep(delay)
                    continue
            print(f"[ocr] {msg}")
            report(msg)
            return None, plan, attempts
        except Exception as exc:
            attempts.append((time.monotonic() - attempt_started,
                             type(exc).__name__))
            msg = f"OpenCode Go API call failed: {exc}"
            # Same rule as an HTTP failure on a field-carrying rung: drop it
            # and try the next one (the last rung is the plain request), so a
            # transient connection error cannot cost the read its only chance.
            if plan and index + 1 < len(plans):
                _reject_plan(plan, f"the call failed ({type(exc).__name__}: "
                                   f"{str(exc)[:100]})")
                index += 1
                continue
            print(f"[ocr] {msg}")
            report(msg)
            return None, plan, attempts


def _log_timing(
    file_path,
    elapsed: float,
    data: Optional[dict],
    thinking: str = "",
    render_s: float = 0.0,
    attempts: Optional[List[tuple]] = None,
    payload_bytes: int = 0,
) -> None:
    """Log one line per read: total time, where it went, what the model spent.

    This is the line that answers "why was that one slow?" — the total the
    user waited, the render time that used to be invisible, every HTTP attempt
    with its own seconds, the payload size, and how many tokens (and how many
    of them reasoning) the model produced.
    """
    bits = [f"read in {elapsed:.1f}s"]
    if render_s:
        bits.append(f"render {render_s:.1f}s")
    if payload_bytes:
        bits.append(f"image {payload_bytes / 1e6:.2f}MB")
    if attempts:
        if len(attempts) > 1:
            bits.append(f"{len(attempts)} attempts (" + "; ".join(
                f"{secs:.1f}s {status}" for secs, status in attempts) + ")")
        elif attempts[0][1] != "ok":
            bits.append(f"attempt {attempts[0][1]}")
    if thinking:
        bits.append(f"mode={thinking}")
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    tokens = usage.get("completion_tokens")
    if isinstance(tokens, int):
        reasoning = _reasoning_tokens(data)
        bits.append(f"{tokens} tokens"
                    + (f" ({reasoning} reasoning)" if reasoning else ""))
        # The model's own decode rate, measured between the first and the last
        # streamed delta — the number "300 tok/s on GLM 5.3 Flash" refers to.
        decode = float(getattr(_STREAM_STATS, "decode_s", 0.0) or 0.0)
        if tokens > 1 and decode > 0.05:
            bits.append(f"{tokens / decode:.0f} tok/s")
    try:
        name = Path(file_path).name
    except Exception:
        name = str(file_path)
    print(f"[ocr] {name}: " + ", ".join(bits))


class OcrPool:
    """Background OCR pool that reads a whole batch of files AT ONCE.

    Every file that lands in the watch folder is submitted the moment it
    arrives (see main.py), and the pool spawns one worker per queued file —
    up to :data:`MAX_CONCURRENT_OCR` — so ten downloads that land together
    are ten simultaneous vision calls, not a queue that trickles. By the time
    a popup opens its result is normally already cached, and no file ever
    shows "reading document…" merely because it was still waiting behind the
    files the user already handled.

    Results are cached by resolved path; callers either poll :meth:`get` /
    :meth:`finished` or register a completion callback with :meth:`submit`.
    Never raises: every failure surfaces as a ``None`` result. Workers are
    daemon threads so the app can always quit immediately, even mid-call.
    """

    # Sentinel pushed on shutdown to stop the workers.
    _STOP = object()

    def __init__(
        self,
        token: Optional[str],
        model: str = OCR_MODEL,
        api_base: str = OCR_API_BASE,
        max_concurrent: int = MAX_CONCURRENT_OCR,
        reasoning_effort: Optional[str] = None,
        thinking: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        self._token = token
        self._model = model
        self._api_base = api_base
        self._provider = provider
        self._max = max(1, max_concurrent)
        # How hard the model may think per read (see OCR_THINKING). "off" is
        # the default because thinking is what made a read take 30s+; the
        # ladder in extract_delivery_note degrades gracefully if the gateway
        # refuses it. `reasoning_effort` is the legacy spelling. A single
        # submission may override it (the popup's retry reads carefully — see
        # retry_thinking_level).
        self._thinking = thinking if thinking is not None else reasoning_effort
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._results: Dict[str, Optional[Dict[str, Optional[str]]]] = {}
        # When each file was submitted, so the log can say how long it waited
        # for a free worker before its vision call even started (a wait that
        # used to be invisible: the popup only showed the read itself).
        self._submitted_at: Dict[str, float] = {}
        # Per-file failure message (only when the last run FAILED — set by
        # the worker from extract_delivery_note's on_error callback). Used
        # by the popup to say WHY OCR failed and offer a retry.
        self._errors: Dict[str, str] = {}
        # Per-file wall-clock seconds of the last read (for the popup status).
        self._durations: Dict[str, float] = {}
        self._active: set = set()          # paths queued or running
        self._waiters: Dict[str, List[Callable]] = {}
        # Worker threads: ONE PER SUBMITTED FILE, up to _max. A file that
        # lands therefore gets its own thread and starts its vision call
        # immediately (a burst of N downloads is read N-at-a-time), while the
        # threads that are not busy simply wait for the next submission.
        # Submissions made after the ceiling is reached wait in the queue for
        # a worker to free up.
        self._workers: List[threading.Thread] = []
        self._stopped = False

    @property
    def available(self) -> bool:
        """True when an API key is present so OCR can actually run."""
        return bool(self._token)

    def _key(self, file_path) -> str:
        """Cache key for *file_path*: the resolved path PLUS the file's identity.

        A path alone is NOT an identity. The scanner drop folder — and any
        download that re-uses a name — writes the same path again (the user's
        log has ``dc.pdf``, and the app moves the file away on save, so the
        next scan creates that exact path anew), and keying the cache by path
        alone meant the new document was served the PREVIOUS file's read: the
        popup filled itself with the last file's company/client/site/materials
        with no API call at all ("it reused fields of last file for a separate
        file... cached ocr"). Size + mtime make the cache follow the document
        instead of the name, so a new file at an old path is read again while
        the same untouched file is still served from cache.

        A file that cannot be stat'ed (already moved away, or deleted while
        its popup was open) falls back to the plain path, so a lookup still
        works for a read that already finished.
        """
        path = Path(file_path)
        try:
            stat = path.stat()
            return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            return str(path.resolve())

    def get(self, file_path) -> Optional[Dict[str, Optional[str]]]:
        """The cached OCR result for *file_path* (None if not finished yet)."""
        with self._lock:
            return self._results.get(self._key(file_path))

    def finished(self, file_path) -> bool:
        """True once *file_path* has an outcome — INCLUDING a failed read.

        :meth:`get` returns ``None`` both for "not read yet" and for "read,
        but nothing could be extracted", which made the popup show a
        "reading document…" line for a file that was already done (and then
        flip straight to the failure). This tells the two apart.
        """
        with self._lock:
            return self._key(file_path) in self._results

    def progress(self):
        """Live counters ``(running, queued)`` for the popup's status line.

        ``running`` is how many reads are in flight right now and ``queued``
        how many submitted files are still waiting for a free worker (always
        0 while the batch fits under :data:`MAX_CONCURRENT_OCR`).
        """
        with self._lock:
            queued = self._queue.qsize()
            running = max(0, len(self._active) - queued)
        return running, queued

    def get_error(self, file_path) -> Optional[str]:
        """The failure message for *file_path*'s LAST run, if it failed.

        Returns None when the last run succeeded (or is still in flight).
        The popup shows this to the user ("OCR failed (API error 500) …")
        and offers the retry button.
        """
        with self._lock:
            return self._errors.get(self._key(file_path))

    def duration(self, file_path) -> Optional[float]:
        """Seconds the last read of *file_path* took (None when unknown).

        Shown by the popup next to the filled fields ("fields filled in
        3.2s"), so the effect of the speed settings is visible at a glance.
        """
        with self._lock:
            return self._durations.get(self._key(file_path))

    def retry(self, file_path, on_done: Optional[Callable] = None,
              thinking: Optional[str] = None) -> bool:
        """Forget any cached result/error for *file_path* and re-run OCR.

        Used by the popup's "↻ Retry OCR" button: the stale cache entry (a
        failure, or an earlier read the user wants replaced) is dropped so a
        fresh vision call actually happens, and the new result (or error)
        replaces it when done. Queueing semantics are identical to
        :meth:`submit`: an already-running file just gets another waiter.
        Returns True when a new call was queued.

        *thinking* overrides the pool's level for THIS read only — the retry
        is a deliberate second look and thinks (see retry_thinking_level).
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
            self._results.pop(key, None)
            self._errors.pop(key, None)
            if key in self._active:
                if on_done is not None:
                    self._waiters.setdefault(key, []).append(on_done)
                return False
            self._active.add(key)
            if on_done is not None:
                self._waiters.setdefault(key, []).append(on_done)
            self._submitted_at[key] = time.monotonic()
            self._queue.put((Path(file_path), key, thinking))
            self._ensure_worker()
            return True

    def submit(self, file_path, on_done: Optional[Callable] = None,
               thinking: Optional[str] = None) -> bool:
        """Queue OCR for *file_path* (no-op when already queued or finished).

        If the result is already cached, *on_done* fires immediately on the
        calling thread. Otherwise it fires (once, from a worker thread) when
        the OCR call completes. Returns True when the file was newly queued.

        *thinking* overrides the pool's level for this read only (see
        :meth:`retry`); None means the pool's configured level.
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
                self._submitted_at[key] = time.monotonic()
                self._queue.put((Path(file_path), key, thinking))
                self._ensure_worker()
                return True
        if done and on_done is not None:
            try:
                on_done(result)
            except Exception:
                pass
        return False

    def _ensure_worker(self) -> None:
        """Start a worker for this submission (up to the concurrency ceiling).

        Called with ``self._lock`` held, right after a file is queued: every
        file gets its own thread until :data:`MAX_CONCURRENT_OCR` workers
        exist, so a whole batch of downloads is read simultaneously instead of
        one wave at a time. Deliberately NOT an idle-worker bookkeeping
        scheme — a stale "worker is free" count could leave a queued file
        waiting behind a busy one (which is exactly the "the next file says
        processing OCR again" symptom). Threads that are not busy just block
        on the queue; files submitted after the ceiling wait for a free
        worker.
        """
        if self._stopped or len(self._workers) >= self._max:
            return
        worker = threading.Thread(
            target=self._worker, daemon=True,
            name=f"filepicker-ocr-{len(self._workers)}")
        self._workers.append(worker)
        worker.start()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                self._queue.task_done()
                return
            path, key, thinking = item
            try:
                self._work(path, key, thinking)
            except Exception as exc:
                print(f"[ocr] worker error for {path}: {exc}")
            finally:
                self._queue.task_done()

    def _work(self, file_path: Path, key: str,
              thinking: Optional[str] = None) -> None:
        level = thinking if thinking is not None else self._thinking
        try:
            # How long this file sat in the queue before a worker picked it up.
            # With a batch larger than the concurrency ceiling this can be the
            # dominant part of "why is this file still reading?", and it was
            # previously invisible in both the log and the popup.
            with self._lock:
                queued_at = self._submitted_at.pop(key, None)
            waited = max(0.0, time.monotonic() - queued_at) if queued_at else 0.0
            print(f"[ocr] reading {file_path.name} …"
                  + (f" (queued {waited:.1f}s)" if waited >= 0.5 else ""))
            errors: List[str] = []
            started = time.monotonic()
            result = extract_delivery_note(
                file_path, token=self._token, model=self._model,
                api_base=self._api_base, on_error=errors.append,
                thinking=level, provider=getattr(self, "_provider", None),
            )
            elapsed = time.monotonic() - started
        except Exception as exc:  # belt & braces: extract never raises
            print(f"[ocr] OCR error for {file_path}: {exc}")
            result = None
            elapsed = time.monotonic() - started
        if result and any(result.values()):
            print(f"[ocr] {file_path.name}: company={result.get('company')!r} "
                  f"client={result.get('client')!r} site={result.get('site')!r} "
                  f"serial={result.get('serial')!r} "
                  f"goods={result.get('goods')!r}")
        else:
            print(f"[ocr] {file_path.name}: no fields extracted")
        with self._lock:
            self._results[key] = result
            self._durations[key] = elapsed
            if errors:
                self._errors[key] = errors[-1]
            else:
                self._errors.pop(key, None)
            self._active.discard(key)
            waiters = list(self._waiters.pop(key, []))
        for cb in waiters:
            try:
                cb(result)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop the workers (in-flight calls finish; queued ones are dropped)."""
        with self._lock:
            self._stopped = True
            workers = list(self._workers)
        for _ in workers:
            try:
                self._queue.put(self._STOP)
            except Exception:
                pass


if __name__ == "__main__":
    # CLI for testing:  python ocr.py <delivery-note.pdf|image> [model] [api_base]
    if len(sys.argv) < 2:
        print("usage: python ocr.py <file.pdf|file.png> [model] [api_base]")
        sys.exit(2)
    from config import _read_ocr_token

    token = _read_ocr_token()
    if not token:
        print("No OCR API key found (env FILEPICKER_COMMANDCODE_TOKEN / "
              "COMMANDCODE_API_KEY / commandcode_token.txt).")
        sys.exit(1)
    model = sys.argv[2] if len(sys.argv) > 2 else OCR_MODEL
    api_base = sys.argv[3] if len(sys.argv) > 3 else OCR_API_BASE
    out = extract_delivery_note(sys.argv[1], token=token, model=model, api_base=api_base)
    if out is None:
        print("OCR failed (see logs above).")
        sys.exit(1)
    for key in ("company", "client", "site", "serial"):
        print(f"{key}: {out.get(key)}")