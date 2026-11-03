"""OpenAI vision helpers: XRD classification, digitizability, layout, ClipDrop.

Standalone helpers for the scrape-and-digitize pipeline. Does not use the
native XRD digitizer or agent-guidance stack.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import cv2
import numpy as np
import requests

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CACHE_DIR = Path(".cache") / "figure_triage"

# Bump when classify/triage prompts or response schemas change (invalidates cache).
XRD_CLASSIFY_PROMPT_VERSION = "v2_high_recall"
TRIAGE_PROMPT_VERSION = "v1"

HttpPost = Callable[..., Any]

DigitizationRoute = Literal["single", "stacked", "multipanel", "unsupported", "reject"]
ClassifyConfidence = Literal["high", "low"]

XRD_CLASSIFY_SYSTEM_PROMPT = """\
You classify scientific figure images from research papers.
Return structured JSON only — no markdown fences.
Decide whether the figure is XRD / X-ray diffraction related.
Prioritize HIGH RECALL: when unsure, prefer keeping the figure for fuller
analysis rather than discarding it.
When a figure caption is provided, use it together with the image —
especially when the crop alone is ambiguous.
"""

XRD_CLASSIFY_USER_PROMPT = """\
Inspect this cropped figure image and return JSON with this schema:
{
  "is_xrd": <bool>,
  "confidence": "high" | "low",
  "reason": "<short explanation>"
}

Rules:
- is_xrd is true if the image is (or clearly includes) an X-ray diffraction /
  powder diffraction / PXRD / Rietveld / 2θ / θ–2θ diffractogram plot.
  false for clear SEM, TEM, FTIR, XPS, photographs, schematics, tables, or
  other non-XRD plots.
- Multi-panel / atypical figures: if ANY panel may be XRD, or the crop mixes
  XRD with other modalities, set is_xrd=true. Prefer recall over precision.
- confidence:
  - "high" when the decision is clear from image and/or caption.
  - "low" when ambiguous, atypical, low-res, multipanel with unclear panels,
    partial axes, stick patterns that might be XRD, or caption/image conflict.
  Downstream runs expensive enrichment on is_xrd=true OR confidence="low".
- When a caption is supplied, weigh it with the image (e.g. captions mentioning
  XRD / PXRD / 2θ / diffraction support is_xrd=true; SEM/TEM/FTIR captions
  support false). Prefer the image if caption and image clearly conflict.
- reason: one short sentence.
"""


def build_xrd_classify_user_prompt(*, caption: str | None = None) -> str:
    """Assemble the XRD classify user prompt with optional caption context."""
    caption_clean = (caption or "").strip()
    if not caption_clean:
        return XRD_CLASSIFY_USER_PROMPT
    return (
        f"{XRD_CLASSIFY_USER_PROMPT.rstrip()}\n\n"
        f"Figure caption:\n{caption_clean}\n"
    )


PREPROCESSING_REASONS = frozenset(
    {
        "multiple_panels",
        "multiple_curves",
        "vertically_offset_curves",
        "overlapping_annotations",
    }
)

# Canonical layouts used for branching.
CANONICAL_LAYOUTS = frozenset(
    {
        "single",
        "multiple_stacked",
        "multiple_overlapping",
        "multiple_subplots",
        "not_applicable",
    }
)

# Accept legacy / short aliases from older prompts and model responses.
_LAYOUT_ALIASES: dict[str, str] = {
    "single": "single",
    "stacked": "multiple_stacked",
    "multiple_stacked": "multiple_stacked",
    "overlay": "multiple_overlapping",
    "overlapping": "multiple_overlapping",
    "multiple_overlapping": "multiple_overlapping",
    "subplots": "multiple_subplots",
    "multiple_subplots": "multiple_subplots",
    "other": "not_applicable",
    "not_applicable": "not_applicable",
    "n/a": "not_applicable",
    "na": "not_applicable",
}

TRIAGE_SYSTEM_PROMPT = """\
You analyze scientific figure images (often XRD / diffraction plots).
Return structured JSON only — no markdown fences.
Classify curve layout, estimate curve count, decide whether the figure is
digitizable (single curve or vertically stacked shared-x curves), extract
curve labels when readable, and say whether in-plot text must be removed
before digitizing.
"""

TRIAGE_USER_PROMPT = """\
Inspect this figure image and return JSON with this schema:
{
  "digitizable": <bool>,
  "curve_count": <int>,
  "curve_layout": "single" | "multiple_stacked" | "multiple_overlapping" | "multiple_subplots" | "not_applicable",
  "shared_x_axis": <bool>,
  "vertically_offset": <bool>,
  "curves_cross": <bool>,
  "needs_clipdrop": <bool>,
  "curve_labels": [{"text": "<label>", "vertical_order": <int>}, ...],
  "reason": "<short explanation>"
}

Rules:
- digitizable is true if:
  (a) exactly one data curve in one axes frame (curve_layout="single"), OR
  (b) multiple vertically stacked traces that share one x-axis
      (curve_layout="multiple_stacked", shared_x_axis=true, curves_cross=false).
  Set digitizable false for overlapping/crossing overlays, separate subplot
  frames that need independent axes, photos, tables, or non-line plots.
- curve_count: number of distinct data curves / traces visible (estimated).
- curve_layout (prefer these canonical values; aliases "stacked"/"overlay"/"other"
  are also accepted by the parser):
  - "single" — one curve in one axes frame
  - "multiple_stacked" — multiple curves in vertically offset bands, same axes
  - "multiple_overlapping" — multiple curves sharing axes and overlapping in y
  - "multiple_subplots" — separate subplot panels with their own frames/axes
  - "not_applicable" — not a digitizable line/spectrum plot
- shared_x_axis: true when all curves share one horizontal axis scale.
- vertically_offset: true when stacked traces are artificially shifted in y.
- curves_cross: true if traces cross or heavily interleave in the same band.
- needs_clipdrop: true if in-plot text annotations (Miller indices, peak labels,
  inset labels, etc.) overlap or sit on the curve / plot interior in a way that
  would interfere with automatic curve tracing. Axis tick labels and axis titles
  outside the plot interior do NOT require ClipDrop. false if the plot interior
  is clean enough to digitize without text removal.
- curve_labels: readable per-curve labels ordered top→bottom (vertical_order 0
  at top). Examples: "715 nm", "(a)", "as-prepared". Empty list if none.
- reason: one short sentence.
"""


@dataclass
class CurveLabelHint:
    text: str
    vertical_order: int


@dataclass
class XrdClassifyResult:
    is_xrd: bool
    confidence: ClassifyConfidence = "high"
    reason: str = ""
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.raw is None:
            payload.pop("raw", None)
        return payload

    @property
    def uncertain(self) -> bool:
        return self.confidence == "low"

    @property
    def needs_full_analysis(self) -> bool:
        """High-recall gate: enrich confirmed XRD and uncertain crops."""
        return bool(self.is_xrd) or self.uncertain

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "XrdClassifyResult | None":
        if not isinstance(payload, dict) or "is_xrd" not in payload:
            return None
        return parse_xrd_classify_payload(payload)


@dataclass
class FigureTriageResult:
    digitizable: bool
    curve_count: int
    curve_layout: str
    needs_clipdrop: bool
    reason: str
    shared_x_axis: bool = True
    vertically_offset: bool = False
    curves_cross: bool = False
    curve_labels: list[CurveLabelHint] = field(default_factory=list)
    preprocessing_reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.raw is None:
            payload.pop("raw", None)
        return payload


def normalize_curve_layout(raw_layout: str | None) -> str:
    """Map model / legacy layout strings to a canonical layout value."""
    key = str(raw_layout or "not_applicable").strip().lower() or "not_applicable"
    return _LAYOUT_ALIASES.get(key, "not_applicable")


def is_digitizable(result: FigureTriageResult) -> bool:
    """True only for a single-curve plot suitable for the existing single path."""
    layout = normalize_curve_layout(result.curve_layout)
    return (
        bool(result.digitizable)
        and layout == "single"
        and int(result.curve_count) == 1
    )


def is_stacked_digitizable(result: FigureTriageResult) -> bool:
    """True for vertically stacked shared-x figures suitable for stacked path."""
    layout = normalize_curve_layout(result.curve_layout)
    if layout != "multiple_stacked":
        return False
    if result.curves_cross:
        return False
    if not result.shared_x_axis:
        return False
    if int(result.curve_count) < 2:
        return False
    return True


def is_multipanel_digitizable(result: FigureTriageResult) -> bool:
    """True when multi-subplot figures should be split then digitized per panel."""
    layout = normalize_curve_layout(result.curve_layout)
    if layout != "multiple_subplots":
        return False
    # Split first; per-panel triage rejects stick/non-spectrum panels.
    return int(result.curve_count) >= 1


def digitization_route(result: FigureTriageResult) -> DigitizationRoute:
    """
    High-level routing decision for the scrape/digitize pipeline.

    Returns:
      - ``single`` — existing single-curve PlotDigitizer path
      - ``stacked`` — stacked multi-curve path
      - ``multipanel`` — split subplot panels, then digitize each
      - ``unsupported`` — multi-curve but not handled yet (overlay)
      - ``reject`` — not digitizable / not applicable
    """
    layout = normalize_curve_layout(result.curve_layout)

    if layout == "single" and is_digitizable(result):
        return "single"

    if layout == "multiple_stacked" and is_stacked_digitizable(result):
        return "stacked"

    if layout == "multiple_subplots" and is_multipanel_digitizable(result):
        return "multipanel"

    if layout == "multiple_subplots":
        return "reject"

    if layout == "multiple_overlapping":
        return "unsupported"

    if layout == "multiple_stacked":
        # Structural mismatch (crossing / no shared x / too few curves).
        if result.curves_cross or not result.shared_x_axis or int(result.curve_count) < 2:
            return "unsupported"
        # Model rejected a geometrically stacked figure (e.g. stick patterns).
        return "reject"

    return "reject"


def _encode_image_png_b64(image_bgr: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise ValueError("Failed to encode image for triage request")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _parse_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Triage response must be a JSON object")
    return data


def _parse_curve_labels(raw_labels: Any) -> list[CurveLabelHint]:
    if not isinstance(raw_labels, list):
        return []
    labels: list[CurveLabelHint] = []
    for index, item in enumerate(raw_labels):
        if isinstance(item, str):
            text = item.strip()
            if text:
                labels.append(CurveLabelHint(text=text, vertical_order=index))
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            order = int(item.get("vertical_order") if item.get("vertical_order") is not None else index)
        except (TypeError, ValueError):
            order = index
        labels.append(CurveLabelHint(text=text, vertical_order=order))
    labels.sort(key=lambda item: item.vertical_order)
    return labels


def parse_triage_payload(raw: dict[str, Any]) -> FigureTriageResult:
    """Normalize a triage JSON object into ``FigureTriageResult``."""
    layout = normalize_curve_layout(str(raw.get("curve_layout") or ""))

    try:
        curve_count = int(raw.get("curve_count") if raw.get("curve_count") is not None else 0)
    except (TypeError, ValueError):
        curve_count = 0
    curve_count = max(0, curve_count)

    shared_x_axis = bool(raw["shared_x_axis"]) if "shared_x_axis" in raw else True
    vertically_offset = bool(raw.get("vertically_offset", layout == "multiple_stacked"))
    curves_cross = bool(raw.get("curves_cross", False))
    curve_labels = _parse_curve_labels(raw.get("curve_labels"))

    digitizable = bool(raw.get("digitizable"))
    # Enforce consistency with layout / count even if the model is inconsistent.
    if layout == "single":
        if curve_count != 1:
            digitizable = False
    elif layout == "multiple_stacked":
        if curve_count < 2 or not shared_x_axis or curves_cross:
            digitizable = False
        elif digitizable is False and curve_count >= 2 and shared_x_axis and not curves_cross:
            # Prefer routing stacked figures when flags look good.
            digitizable = True
    elif layout == "multiple_subplots":
        # Keep the model's digitizable flag; routing still splits panels.
        pass
    else:
        digitizable = False

    preprocessing_reasons: list[str] = []
    raw_reasons = raw.get("preprocessing_reasons")
    if isinstance(raw_reasons, list):
        for item in raw_reasons:
            key = str(item or "").strip().lower()
            if key in PREPROCESSING_REASONS and key not in preprocessing_reasons:
                preprocessing_reasons.append(key)
    if layout == "multiple_subplots" and "multiple_panels" not in preprocessing_reasons:
        preprocessing_reasons.append("multiple_panels")
    if layout == "multiple_stacked" and "vertically_offset_curves" not in preprocessing_reasons:
        preprocessing_reasons.append("vertically_offset_curves")
    if bool(raw.get("needs_clipdrop")) and "overlapping_annotations" not in preprocessing_reasons:
        preprocessing_reasons.append("overlapping_annotations")

    # Store canonical layout; keep short aliases only in raw.
    return FigureTriageResult(
        digitizable=digitizable,
        curve_count=curve_count,
        curve_layout=layout,
        needs_clipdrop=bool(raw.get("needs_clipdrop")),
        reason=str(raw.get("reason") or "").strip(),
        shared_x_axis=shared_x_axis,
        vertically_offset=vertically_offset,
        curves_cross=curves_cross,
        curve_labels=curve_labels,
        preprocessing_reasons=preprocessing_reasons,
        raw=dict(raw),
    )


def _normalize_api_key(raw: str | None) -> str | None:
    """Strip whitespace and wrapping quotes (incl. curly) from an API key.

    Copy-pasted keys often arrive as ``’sk-…’``; HTTP headers must be latin-1,
    so those characters blow up before the request leaves the client.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    quotes = "'\"‘’“”`"
    while len(key) >= 2 and key[0] in quotes and key[-1] in quotes:
        key = key[1:-1].strip()
    # Also drop a lone leading/trailing smart quote from partial paste.
    key = key.strip(quotes + " \t\r\n")
    if not key:
        return None
    try:
        key.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise RuntimeError(
            "OPENAI_API_KEY contains non-ASCII characters after quote stripping; "
            "re-export it without smart quotes (e.g. export OPENAI_API_KEY=sk-...)"
        ) from exc
    return key


def parse_xrd_classify_payload(payload: dict[str, Any]) -> XrdClassifyResult:
    """Normalize an XRD-classification JSON object."""
    raw_conf = str(payload.get("confidence") or "high").strip().lower()
    confidence: ClassifyConfidence = "low" if raw_conf in {"low", "uncertain"} else "high"
    # Legacy caches / callers without confidence: treat as high.
    if "confidence" not in payload and payload.get("uncertain") is True:
        confidence = "low"
    return XrdClassifyResult(
        is_xrd=bool(payload.get("is_xrd")),
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip(),
        raw=dict(payload),
    )


def _cache_enabled(use_cache: bool | None = None) -> bool:
    if use_cache is not None:
        return bool(use_cache)
    raw = os.environ.get("OPENAI_USE_CACHE")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _triage_cache_dir() -> Path:
    override = os.environ.get("OPENAI_CACHE_DIR") or os.environ.get(
        "XRD_TRIAGE_CACHE_DIR"
    )
    if override:
        return Path(override) / "figure_triage"
    return DEFAULT_CACHE_DIR


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_content_hash(image_bgr: np.ndarray) -> str:
    """Stable hash of PNG-encoded pixels (independent of filesystem path)."""
    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        raise ValueError("Failed to encode image for cache key")
    return _sha256_bytes(buf.tobytes())


def _caption_hash(caption: str | None) -> str:
    text = (caption or "").strip().encode("utf-8")
    return _sha256_bytes(text)[:16] if text else "nocap"


def _vision_disk_cache_path(
    *,
    kind: str,
    image_hash: str,
    model: str,
    prompt_version: str,
    caption_hash: str = "nocap",
) -> Path:
    safe_model = re.sub(r"[^\w.-]+", "_", model).strip("_") or "model"
    name = f"{image_hash[:24]}_{safe_model}_{prompt_version}_{caption_hash}.json"
    return _triage_cache_dir() / kind / name


def _read_vision_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_vision_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _openai_chat_json(
    image_bgr: np.ndarray,
    *,
    system_prompt: str,
    user_prompt: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    http_post: HttpPost | None = None,
    missing_key_message: str = "Missing OPENAI_API_KEY",
) -> dict[str, Any]:
    """POST a vision chat.completions request and return parsed JSON content."""
    key = _normalize_api_key(api_key or os.environ.get("OPENAI_API_KEY"))
    if not key:
        raise RuntimeError(missing_key_message)

    url_base = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip(
        "/"
    )
    model_name = model or os.environ.get("OPENAI_TRIAGE_MODEL") or DEFAULT_MODEL
    b64 = _encode_image_png_b64(image_bgr)

    body = {
        "model": model_name,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    post = http_post or requests.post
    response = post(
        f"{url_base}/chat/completions",
        headers=headers,
        json=body,
        timeout=timeout_s,
    )
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected OpenAI response shape: {data!r}") from exc
    if not isinstance(content, str):
        raise ValueError("OpenAI message content must be a string")
    return _parse_json_content(content)


def _load_bgr_image(image: np.ndarray | str | Path) -> np.ndarray:
    if isinstance(image, (str, Path)):
        path = Path(image)
        bgr = cv2.imread(str(path))
        if bgr is None:
            raise FileNotFoundError(f"Could not read image: {path}")
        return bgr
    bgr = np.asarray(image)
    if bgr.ndim != 3 or bgr.shape[2] < 3:
        raise ValueError("Expected a BGR image array")
    return bgr


def _effective_use_cache(
    use_cache: bool | None,
    *,
    http_post: HttpPost | None,
) -> bool:
    """Disk-cache live API calls; skip cache for injected http_post unless forced on."""
    if use_cache is not None:
        return bool(use_cache)
    if http_post is not None:
        return False
    return _cache_enabled(None)


def call_openai_xrd_classify(
    image_bgr: np.ndarray,
    *,
    caption: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    http_post: HttpPost | None = None,
    use_cache: bool | None = None,
) -> XrdClassifyResult:
    """Call an OpenAI-compatible vision endpoint for XRD vs non-XRD classification."""
    model_name = model or os.environ.get("OPENAI_TRIAGE_MODEL") or DEFAULT_MODEL
    cache_on = _effective_use_cache(use_cache, http_post=http_post)
    cache_path = _vision_disk_cache_path(
        kind="xrd_classify",
        image_hash=_image_content_hash(image_bgr),
        model=model_name,
        prompt_version=XRD_CLASSIFY_PROMPT_VERSION,
        caption_hash=_caption_hash(caption),
    )
    if cache_on:
        cached = _read_vision_cache(cache_path)
        if cached is not None:
            LOGGER.info("XRD classify cache hit: %s", cache_path.name)
            return parse_xrd_classify_payload(cached)

    payload = _openai_chat_json(
        image_bgr,
        system_prompt=XRD_CLASSIFY_SYSTEM_PROMPT,
        user_prompt=build_xrd_classify_user_prompt(caption=caption),
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        timeout_s=timeout_s,
        http_post=http_post,
        missing_key_message="Missing OPENAI_API_KEY for XRD classification",
    )
    result = parse_xrd_classify_payload(payload)
    if cache_on:
        _write_vision_cache(cache_path, result.to_dict())
    return result


def classify_xrd_figure_image(
    image: np.ndarray | str | Path,
    *,
    caption: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_post: HttpPost | None = None,
    use_cache: bool | None = None,
) -> XrdClassifyResult:
    """Classify whether a figure image is XRD-related via OpenAI vision."""
    return call_openai_xrd_classify(
        _load_bgr_image(image),
        caption=caption,
        api_key=api_key,
        base_url=base_url,
        model=model,
        http_post=http_post,
        use_cache=use_cache,
    )


def call_openai_triage(
    image_bgr: np.ndarray,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout_s: float = 120.0,
    http_post: HttpPost | None = None,
    use_cache: bool | None = None,
) -> FigureTriageResult:
    """Call an OpenAI-compatible vision endpoint for figure triage."""
    model_name = model or os.environ.get("OPENAI_TRIAGE_MODEL") or DEFAULT_MODEL
    cache_on = _effective_use_cache(use_cache, http_post=http_post)
    cache_path = _vision_disk_cache_path(
        kind="triage",
        image_hash=_image_content_hash(image_bgr),
        model=model_name,
        prompt_version=TRIAGE_PROMPT_VERSION,
    )
    if cache_on:
        cached = _read_vision_cache(cache_path)
        if cached is not None:
            LOGGER.info("Triage cache hit: %s", cache_path.name)
            return parse_triage_payload(cached)

    payload = _openai_chat_json(
        image_bgr,
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        user_prompt=TRIAGE_USER_PROMPT,
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        timeout_s=timeout_s,
        http_post=http_post,
        missing_key_message="Missing OPENAI_API_KEY for figure triage",
    )
    result = parse_triage_payload(payload)
    if cache_on:
        # Store the raw model payload when available so re-parse stays consistent.
        _write_vision_cache(cache_path, result.raw or result.to_dict())
    return result


def triage_figure_image(
    image: np.ndarray | str | Path,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_post: HttpPost | None = None,
    use_cache: bool | None = None,
) -> FigureTriageResult:
    """Triage a figure image path or BGR array."""
    return call_openai_triage(
        _load_bgr_image(image),
        api_key=api_key,
        base_url=base_url,
        model=model,
        http_post=http_post,
        use_cache=use_cache,
    )


def save_triage_result(result: FigureTriageResult, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return path
