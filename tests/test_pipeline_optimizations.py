"""Tests for parallel enrich, resume fingerprints, combined triage, and metrics."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy"
SCRIPTS = ROOT / "scripts"
for path in (str(ROOT), str(LEGACY), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from figure_analysis.caption_hints import (  # noqa: E402
    caption_xrd_hint,
)
from figure_analysis.enrich_resume import (  # noqa: E402
    ENRICHMENT_META_KEY,
    build_enrichment_meta,
    can_resume_enrichment,
    image_file_sha256,
)
from figure_analysis.image_util import downscale_bgr, resolve_classify_max_side  # noqa: E402
from figure_analysis.metrics import PipelineMetrics  # noqa: E402
from figure_analysis.schemas import FigureAnalysisResult, XrdCurve  # noqa: E402
from figure_triage import (  # noqa: E402
    parse_combined_classify_triage_payload,
)
import scrape_and_digitize as sad  # noqa: E402
from figure_triage import XrdClassifyResult  # noqa: E402


def _make_png(path: Path, color=(200, 200, 200), size=(24, 24)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class CaptionHintTests(unittest.TestCase):
    def test_soft_hints_not_hard_rules(self) -> None:
        self.assertEqual(caption_xrd_hint("PXRD pattern of TiO2"), "likely_xrd")
        self.assertEqual(caption_xrd_hint("SEM image of the surface"), "likely_non_xrd")
        # Multipanel with XRD stays likely_xrd; multipanel without stays unknown.
        self.assertEqual(
            caption_xrd_hint("(a) SEM image (b) XRD pattern"),
            "likely_xrd",
        )
        self.assertEqual(
            caption_xrd_hint("(a) overview (b) detail of the film"),
            "unknown",
        )


class ImageUtilTests(unittest.TestCase):
    def test_downscale_and_env(self) -> None:
        import numpy as np

        big = np.zeros((2000, 1000, 3), dtype=np.uint8)
        small = downscale_bgr(big, max_side=500)
        self.assertEqual(max(small.shape[:2]), 500)
        with patch.dict(os.environ, {"XRD_CLASSIFY_MAX_SIDE": "0"}):
            self.assertIsNone(resolve_classify_max_side(None))
        with patch.dict(os.environ, {"XRD_CLASSIFY_MAX_SIDE": "1024"}):
            self.assertEqual(resolve_classify_max_side(None), 1024)


class CombinedParseTests(unittest.TestCase):
    def test_split_payload(self) -> None:
        classify, triage = parse_combined_classify_triage_payload(
            {
                "is_xrd": True,
                "confidence": "high",
                "classify_reason": "PXRD",
                "digitizable": True,
                "curve_count": 1,
                "curve_layout": "single",
                "needs_clipdrop": False,
                "triage_reason": "single curve",
            }
        )
        self.assertTrue(classify.is_xrd)
        self.assertEqual(classify.reason, "PXRD")
        self.assertTrue(triage.digitizable)
        self.assertEqual(triage.curve_layout, "single")


class ResumeFingerprintTests(unittest.TestCase):
    def test_can_resume_when_meta_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png = _make_png(Path(tmp) / "fig.png")
            meta = build_enrichment_meta(
                image_hash=image_file_sha256(png),
                classify_model="gpt-4.1-mini",
                analysis_model="gpt-4.1",
            )
            entry = {
                "is_xrd": True,
                "analysis": {"sample": "x", "curves": []},
                ENRICHMENT_META_KEY: meta,
            }
            self.assertTrue(
                can_resume_enrichment(
                    entry,
                    image_path=png,
                    classify_model="gpt-4.1-mini",
                    analysis_model="gpt-4.1",
                    overwrite=False,
                )
            )
            self.assertFalse(
                can_resume_enrichment(
                    entry,
                    image_path=png,
                    classify_model="gpt-4.1-mini",
                    analysis_model="gpt-4.1",
                    overwrite=True,
                )
            )
            entry[ENRICHMENT_META_KEY]["schema_version"] = "old"
            self.assertFalse(
                can_resume_enrichment(
                    entry,
                    image_path=png,
                    classify_model="gpt-4.1-mini",
                    analysis_model="gpt-4.1",
                    overwrite=False,
                )
            )


class ParallelEnrichTests(unittest.TestCase):
    def test_parallel_enrich_preserves_order_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / "paper"
            figures = paper / "figures"
            paths = [
                _make_png(figures / f"fig_{i}.png", (i * 10, i * 10, i * 10))
                for i in range(4)
            ]
            fa = paper / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "authors": [],
                        "figures": [
                            {
                                "figure": i + 1,
                                "figure_id": f"fig_{i}",
                                "figure_path": str(paths[i]),
                                "caption": "PXRD" if i % 2 else "SEM image",
                                "text": "",
                            }
                            for i in range(4)
                        ],
                    }
                ),
                encoding="utf-8",
            )

            active = {"n": 0, "max": 0}
            lock = threading.Lock()

            def fake_classify(image_path, **kwargs):
                with lock:
                    active["n"] += 1
                    active["max"] = max(active["max"], active["n"])
                try:
                    name = Path(image_path).name
                    if "fig_1" in name or "fig_3" in name:
                        return XrdClassifyResult(
                            is_xrd=True, confidence="high", reason="xrd"
                        )
                    return XrdClassifyResult(
                        is_xrd=False, confidence="high", reason="sem"
                    )
                finally:
                    with lock:
                        active["n"] -= 1

            def fake_analyze(image_path, **kwargs):
                return FigureAnalysisResult(
                    is_xrd=True,
                    sample="demo",
                    curves=[XrdCurve(peak_positions=[25.0])],
                )

            metrics = PipelineMetrics()
            with patch.object(sad, "classify_xrd_figure_image", side_effect=fake_classify):
                with patch.object(sad, "analyze_figure_image", side_effect=fake_analyze):
                    sad.enrich_figure_analysis_json(
                        paper,
                        client=object(),
                        workers=4,
                        metrics=metrics,
                    )

            data = json.loads(fa.read_text(encoding="utf-8"))
            self.assertEqual(data["figures_total"], 4)
            self.assertEqual(data["xrd_figures"], 2)
            ids = [e["figure_id"] for e in data["figures"]]
            self.assertEqual(ids, ["fig_1", "fig_3"])
            for entry in data["figures"]:
                self.assertIn(ENRICHMENT_META_KEY, entry)
            self.assertTrue((paper / "enrichment_metrics.json").is_file())
            self.assertGreaterEqual(active["max"], 1)
            self.assertEqual(metrics.gated_not_xrd, 2)
            self.assertEqual(metrics.analyzed_xrd, 2)

    def test_resume_skips_matching_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / "paper"
            figures = paper / "xrd_figures"
            png = _make_png(figures / "fig_x.png")
            meta = build_enrichment_meta(
                image_hash=image_file_sha256(png),
                classify_model="gpt-4.1-mini",
                analysis_model="gpt-4.1",
            )
            fa = paper / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "authors": [],
                        "figures": [
                            {
                                "figure": 1,
                                "figure_id": "fig_x",
                                "figure_path": str(png),
                                "caption": "PXRD",
                                "is_xrd": True,
                                "analysis": {
                                    "sample": "kept",
                                    "curves": [],
                                    "trends": [],
                                    "composition": [],
                                    "space_group": [],
                                    "fwhm": None,
                                    "lattice_parameters": [],
                                    "profile_function": None,
                                },
                                ENRICHMENT_META_KEY: meta,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            classify_calls: list[str] = []
            analyze_calls: list[str] = []

            def fake_classify(image_path, **kwargs):
                classify_calls.append(Path(image_path).name)
                return XrdClassifyResult(is_xrd=True, confidence="high")

            def fake_analyze(image_path, **kwargs):
                analyze_calls.append(Path(image_path).name)
                return FigureAnalysisResult(is_xrd=True, sample="new")

            with patch.object(sad, "classify_xrd_figure_image", side_effect=fake_classify):
                with patch.object(sad, "analyze_figure_image", side_effect=fake_analyze):
                    with patch.dict(
                        os.environ,
                        {
                            "OPENAI_TRIAGE_MODEL": "gpt-4.1-mini",
                            "OPENAI_ANALYSIS_MODEL": "gpt-4.1",
                        },
                    ):
                        sad.enrich_figure_analysis_json(
                            paper,
                            client=object(),
                            workers=1,
                            overwrite=False,
                            triage_model="gpt-4.1-mini",
                            analysis_model="gpt-4.1",
                        )

            self.assertEqual(classify_calls, [])
            self.assertEqual(analyze_calls, [])
            data = json.loads(fa.read_text(encoding="utf-8"))
            self.assertEqual(data["figures"][0]["analysis"]["sample"], "kept")


class MetricsTests(unittest.TestCase):
    def test_cost_and_hit_rate(self) -> None:
        m = PipelineMetrics(classify_model="gpt-4.1-mini", analysis_model="gpt-4.1")
        m.add_stage("classify", api_calls=2, seconds=1.0)
        m.add_stage("classify", cache_hits=2, seconds=0.1)
        m.add_stage("analyze", api_calls=1, seconds=2.0)
        d = m.to_dict()
        self.assertEqual(d["api_calls_total"], 3)
        self.assertEqual(d["cache_hits_total"], 2)
        self.assertGreater(d["estimated_cost_usd"], 0)
        self.assertAlmostEqual(d["cache_hit_rate"], 2 / 5, places=4)


if __name__ == "__main__":
    unittest.main()
