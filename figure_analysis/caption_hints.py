"""Soft caption-based XRD hints (never used as hard exclusion gates)."""

from __future__ import annotations

import re
from typing import Literal

CaptionHint = Literal["likely_xrd", "likely_non_xrd", "unknown"]

_XRD_PATTERNS = (
    r"\bxrd\b",
    r"\bpxrd\b",
    r"\bgxrd\b",
    r"\bx\-?ray\s+diffraction\b",
    r"\bpowder\s+diffraction\b",
    r"\bdiffractogram\b",
    r"\bdiffractometr",
    r"\brietveld\b",
    r"\b2\s*θ\b",
    r"\b2theta\b",
    r"\bθ\s*[–\-]\s*2θ\b",
    r"\bbragg\b",
)

_NON_XRD_PATTERNS = (
    r"\bsem\b",
    r"\btem\b",
    r"\bhrtem\b",
    r"\bstem\b",
    r"\bafm\b",
    r"\bftir\b",
    r"\bxps\b",
    r"\braman\b",
    r"\bnmr\b",
    r"\buv\-?vis\b",
    r"\bphotograph\b",
    r"\bschematic\b",
    r"\boptical\s+image\b",
    r"\bmicroscope\b",
    r"\beds\b",
    r"\bedx\b",
    r"\bmapping\b",
)

# Multipanel / mixed captions: never treat as hard non-XRD from text alone.
_MIXED_PANEL_PATTERNS = (
    r"\(a\)",
    r"\(b\)",
    r"\bpanel",
    r"\bmultipanel\b",
    r"\bmulti\-panel\b",
    r"\bfigures?\s+\d",
)


def caption_xrd_hint(caption: str | None) -> CaptionHint:
    """Return a soft hint from caption text.

    Multipanel / mixed-modality captions never become hard non-XRD exclusions.
    If XRD is mentioned alongside other modalities/panels, prefer likely_xrd
    (recall-first); otherwise stay unknown.
    """
    text = (caption or "").strip().lower()
    if not text:
        return "unknown"

    has_xrd = any(re.search(p, text, flags=re.IGNORECASE) for p in _XRD_PATTERNS)
    has_non = any(re.search(p, text, flags=re.IGNORECASE) for p in _NON_XRD_PATTERNS)
    mixed_panel = any(
        re.search(p, text, flags=re.IGNORECASE) for p in _MIXED_PANEL_PATTERNS
    )

    if mixed_panel or (has_xrd and has_non):
        # Image may still contain XRD — never soft-exclude.
        return "likely_xrd" if has_xrd else "unknown"

    if has_xrd:
        return "likely_xrd"
    if has_non:
        return "likely_non_xrd"
    return "unknown"


def caption_hint_prompt_clause(hint: CaptionHint) -> str:
    """Optional soft guidance appended to classify prompts (not a hard rule)."""
    if hint == "likely_xrd":
        return (
            "Caption hint (soft, not decisive): the caption suggests XRD. "
            "Still inspect the image; if any panel may be XRD prefer is_xrd=true."
        )
    if hint == "likely_non_xrd":
        return (
            "Caption hint (soft, not decisive): the caption suggests a non-XRD "
            "modality. Still inspect the image carefully — multipanel crops or "
            "mis-associated captions must not discard real XRD. Prefer "
            "confidence=\"low\" over a confident false when unsure."
        )
    return ""
