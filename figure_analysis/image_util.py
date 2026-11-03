"""Image helpers for vision API calls."""

from __future__ import annotations

import os
from typing import Optional

import cv2
import numpy as np


def resolve_classify_max_side(override: int | None = None) -> int | None:
    """Max long-edge for classify/combined vision calls.

    ``None`` / ``0`` / negative disables downscaling. Default 1280 via
    ``XRD_CLASSIFY_MAX_SIDE`` (full enrichment still uses the analysis client's
    higher-resolution encoder).
    """
    if override is not None:
        return int(override) if int(override) > 0 else None
    raw = os.environ.get("XRD_CLASSIFY_MAX_SIDE")
    if raw is None:
        return 1280
    raw = raw.strip().lower()
    if raw in {"", "0", "none", "off", "false"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return 1280
    return value if value > 0 else None


def downscale_bgr(
    image_bgr: np.ndarray,
    *,
    max_side: Optional[int] = 1280,
) -> np.ndarray:
    """Return a copy scaled so max(h, w) <= max_side (no-op when already smaller)."""
    if max_side is None or max_side <= 0:
        return image_bgr
    h, w = image_bgr.shape[:2]
    long_edge = max(h, w)
    if long_edge <= max_side:
        return image_bgr
    scale = max_side / float(long_edge)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return cv2.resize(image_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
