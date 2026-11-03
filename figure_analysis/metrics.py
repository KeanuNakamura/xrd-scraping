"""Per-stage timing, API/cache counters, and rough cost estimates."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rough USD per 1M tokens (input/output). Vision image tokens are folded into
# a flat per-call estimate below — good enough for relative A/B measurement.
_MODEL_TOKEN_RATES_USD: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}

# Flat per-vision-call estimates when token usage is unavailable.
_MODEL_VISION_CALL_USD: dict[str, float] = {
    "gpt-4.1": 0.02,
    "gpt-4.1-mini": 0.004,
    "gpt-4o": 0.02,
    "gpt-4o-mini": 0.003,
}


def _rate_for(model: str) -> float:
    key = (model or "").strip().lower()
    if key in _MODEL_VISION_CALL_USD:
        return _MODEL_VISION_CALL_USD[key]
    for prefix, rate in _MODEL_VISION_CALL_USD.items():
        if key.startswith(prefix):
            return rate
    return 0.01


@dataclass
class PipelineMetrics:
    """Thread-safe counters for one enrich / digitize pass."""

    classify_api_calls: int = 0
    classify_cache_hits: int = 0
    classify_seconds: float = 0.0

    analyze_api_calls: int = 0
    analyze_cache_hits: int = 0
    analyze_seconds: float = 0.0

    triage_api_calls: int = 0
    triage_cache_hits: int = 0
    triage_seconds: float = 0.0

    combined_api_calls: int = 0
    combined_cache_hits: int = 0
    combined_seconds: float = 0.0

    paper_api_calls: int = 0
    paper_cache_hits: int = 0
    paper_seconds: float = 0.0

    skipped_resume: int = 0
    gated_not_xrd: int = 0
    analyzed_xrd: int = 0
    analyzed_not_xrd: int = 0
    failed: int = 0
    missing: int = 0

    caption_hint_likely_xrd: int = 0
    caption_hint_likely_non_xrd: int = 0
    caption_hint_unknown: int = 0

    classify_model: str = ""
    analysis_model: str = ""
    workers: int = 1
    overwrite: bool = False
    downscale_classify_max_side: int | None = None

    wall_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _t0: float = field(default_factory=time.time, repr=False)

    def add_stage(
        self,
        stage: str,
        *,
        seconds: float = 0.0,
        api_calls: int = 0,
        cache_hits: int = 0,
    ) -> None:
        with self._lock:
            if stage == "classify":
                self.classify_seconds += seconds
                self.classify_api_calls += api_calls
                self.classify_cache_hits += cache_hits
            elif stage == "analyze":
                self.analyze_seconds += seconds
                self.analyze_api_calls += api_calls
                self.analyze_cache_hits += cache_hits
            elif stage == "triage":
                self.triage_seconds += seconds
                self.triage_api_calls += api_calls
                self.triage_cache_hits += cache_hits
            elif stage == "combined":
                self.combined_seconds += seconds
                self.combined_api_calls += api_calls
                self.combined_cache_hits += cache_hits
            elif stage == "paper":
                self.paper_seconds += seconds
                self.paper_api_calls += api_calls
                self.paper_cache_hits += cache_hits

    def bump(self, field_name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + n)

    def finish(self) -> None:
        with self._lock:
            self.wall_seconds = round(time.time() - self._t0, 3)

    def estimated_cost_usd(self) -> float:
        classify_calls = self.classify_api_calls + self.combined_api_calls
        triage_only = self.triage_api_calls
        analyze = self.analyze_api_calls + self.paper_api_calls
        triage_model = self.classify_model or "gpt-4.1-mini"
        analysis_model = self.analysis_model or "gpt-4.1"
        return round(
            classify_calls * _rate_for(triage_model)
            + triage_only * _rate_for(triage_model)
            + analyze * _rate_for(analysis_model),
            4,
        )

    def cache_hit_rate(self) -> float:
        hits = (
            self.classify_cache_hits
            + self.analyze_cache_hits
            + self.triage_cache_hits
            + self.combined_cache_hits
            + self.paper_cache_hits
        )
        calls = (
            self.classify_api_calls
            + self.analyze_api_calls
            + self.triage_api_calls
            + self.combined_api_calls
            + self.paper_api_calls
        )
        total = hits + calls
        return round(hits / total, 4) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        self.finish()
        # Avoid dataclasses.asdict — it deep-copies and cannot pickle Lock.
        payload = {
            "classify_api_calls": self.classify_api_calls,
            "classify_cache_hits": self.classify_cache_hits,
            "classify_seconds": round(self.classify_seconds, 3),
            "analyze_api_calls": self.analyze_api_calls,
            "analyze_cache_hits": self.analyze_cache_hits,
            "analyze_seconds": round(self.analyze_seconds, 3),
            "triage_api_calls": self.triage_api_calls,
            "triage_cache_hits": self.triage_cache_hits,
            "triage_seconds": round(self.triage_seconds, 3),
            "combined_api_calls": self.combined_api_calls,
            "combined_cache_hits": self.combined_cache_hits,
            "combined_seconds": round(self.combined_seconds, 3),
            "paper_api_calls": self.paper_api_calls,
            "paper_cache_hits": self.paper_cache_hits,
            "paper_seconds": round(self.paper_seconds, 3),
            "skipped_resume": self.skipped_resume,
            "gated_not_xrd": self.gated_not_xrd,
            "analyzed_xrd": self.analyzed_xrd,
            "analyzed_not_xrd": self.analyzed_not_xrd,
            "failed": self.failed,
            "missing": self.missing,
            "caption_hint_likely_xrd": self.caption_hint_likely_xrd,
            "caption_hint_likely_non_xrd": self.caption_hint_likely_non_xrd,
            "caption_hint_unknown": self.caption_hint_unknown,
            "classify_model": self.classify_model,
            "analysis_model": self.analysis_model,
            "workers": self.workers,
            "overwrite": self.overwrite,
            "downscale_classify_max_side": self.downscale_classify_max_side,
            "wall_seconds": self.wall_seconds,
        }
        payload["estimated_cost_usd"] = self.estimated_cost_usd()
        payload["cache_hit_rate"] = self.cache_hit_rate()
        payload["api_calls_total"] = (
            self.classify_api_calls
            + self.analyze_api_calls
            + self.triage_api_calls
            + self.combined_api_calls
            + self.paper_api_calls
        )
        payload["cache_hits_total"] = (
            self.classify_cache_hits
            + self.analyze_cache_hits
            + self.triage_cache_hits
            + self.combined_cache_hits
            + self.paper_cache_hits
        )
        return payload

    def write_json(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def log_summary(self, log: logging.Logger | None = None) -> None:
        log = log or logger
        d = self.to_dict()
        log.info(
            "Pipeline metrics: wall=%.1fs api=%d cache_hits=%d hit_rate=%.1f%% "
            "est_cost=$%.4f (classify=%d/%d analyze=%d/%d triage=%d/%d "
            "combined=%d/%d resume_skip=%d)",
            d["wall_seconds"],
            d["api_calls_total"],
            d["cache_hits_total"],
            100.0 * d["cache_hit_rate"],
            d["estimated_cost_usd"],
            self.classify_api_calls,
            self.classify_cache_hits,
            self.analyze_api_calls,
            self.analyze_cache_hits,
            self.triage_api_calls,
            self.triage_cache_hits,
            self.combined_api_calls,
            self.combined_cache_hits,
            self.skipped_resume,
        )
