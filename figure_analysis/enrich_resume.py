"""Fingerprint helpers for enrichment resume / skip."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from figure_analysis.versions import (
    ANALYSIS_PROMPT_VERSION,
    ANALYSIS_SCHEMA_VERSION,
    XRD_CLASSIFY_PROMPT_VERSION,
)

ENRICHMENT_META_KEY = "enrichment_meta"


def image_file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Content hash of an on-disk image (independent of path)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_enrichment_meta(
    *,
    image_hash: str,
    classify_model: str,
    analysis_model: str,
    classify_prompt_version: str = XRD_CLASSIFY_PROMPT_VERSION,
    analysis_prompt_version: str = ANALYSIS_PROMPT_VERSION,
    schema_version: str = ANALYSIS_SCHEMA_VERSION,
) -> dict[str, str]:
    return {
        "image_hash": image_hash,
        "classify_model": classify_model,
        "analysis_model": analysis_model,
        "classify_prompt_version": classify_prompt_version,
        "analysis_prompt_version": analysis_prompt_version,
        "schema_version": schema_version,
    }


def enrichment_is_complete(entry: dict[str, Any]) -> bool:
    """True when the entry already has a successful XRD enrichment payload."""
    if not entry.get("is_xrd"):
        return False
    analysis = entry.get("analysis")
    if not isinstance(analysis, dict):
        return False
    if entry.get("association_status") in {"analysis_failed", "missing_image"}:
        return False
    if entry.get("analysis_error"):
        return False
    return True


def enrichment_meta_matches(
    entry: dict[str, Any],
    expected: dict[str, str],
) -> bool:
    stored = entry.get(ENRICHMENT_META_KEY)
    if not isinstance(stored, dict):
        return False
    for key, value in expected.items():
        if str(stored.get(key) or "") != str(value):
            return False
    return True


def can_resume_enrichment(
    entry: dict[str, Any],
    *,
    image_path: Path,
    classify_model: str,
    analysis_model: str,
    overwrite: bool,
) -> bool:
    """Skip re-classify/re-analyze when fingerprints still match."""
    if overwrite:
        return False
    if not enrichment_is_complete(entry):
        return False
    try:
        image_hash = image_file_sha256(image_path)
    except OSError:
        return False
    expected = build_enrichment_meta(
        image_hash=image_hash,
        classify_model=classify_model,
        analysis_model=analysis_model,
    )
    return enrichment_meta_matches(entry, expected)
