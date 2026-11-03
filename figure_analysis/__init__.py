"""Compact XRD scientific figure metadata extraction (vision → structured JSON).

Extracts dataset-friendly XRD metadata (peaks, phases, hkl, FWHM/broadening,
conditions). Does NOT digitize curves or reconstruct XY spectra.
"""

from __future__ import annotations

from figure_analysis.analyzer import analyze_figure_image, analyze_paper
from figure_analysis.config import (
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_TRIAGE_MODEL,
    resolve_analysis_model,
    resolve_triage_model,
)
from figure_analysis.prompts import compact_paper_payload_for_analysis
from figure_analysis.schemas import (
    EvidenceCitation,
    FigureAnalysisPayload,
    FigureAnalysisResult,
    PaperAnalysis,
    ScientificInterpretation,
)

__all__ = [
    "DEFAULT_ANALYSIS_MODEL",
    "DEFAULT_TRIAGE_MODEL",
    "EvidenceCitation",
    "FigureAnalysisPayload",
    "FigureAnalysisResult",
    "PaperAnalysis",
    "ScientificInterpretation",
    "analyze_figure_image",
    "analyze_paper",
    "compact_paper_payload_for_analysis",
    "resolve_analysis_model",
    "resolve_triage_model",
]
