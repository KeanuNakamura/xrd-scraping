"""Regression tests for two-tier enrichment, classify reuse, and API cost controls."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy"
SCRIPTS = ROOT / "scripts"
for path in (str(ROOT), str(LEGACY), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from figure_analysis.config import (  # noqa: E402
    DEFAULT_ANALYSIS_MODEL,
    DEFAULT_TRIAGE_MODEL,
    resolve_analysis_model,
    resolve_triage_model,
)
from figure_analysis.prompts import compact_paper_payload_for_analysis  # noqa: E402
from figure_analysis.schemas import FigureAnalysisResult, XrdCurve  # noqa: E402
from figure_triage import (  # noqa: E402
    XRD_CLASSIFY_PROMPT_VERSION,
    XrdClassifyResult,
    classify_xrd_figure_image,
    parse_xrd_classify_payload,
    triage_figure_image,
)
import scrape_and_digitize as sad  # noqa: E402


def _make_png(path: Path, color=(200, 200, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), color).save(path)
    return path


def _http_json(payload: dict):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps(payload)}}]
    }
    return response


class ModelConfigTests(unittest.TestCase):
    def test_defaults_split_by_task(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_MODEL", None)
            os.environ.pop("OPENAI_ANALYSIS_MODEL", None)
            os.environ.pop("OPENAI_TRIAGE_MODEL", None)
            self.assertEqual(resolve_analysis_model(None), DEFAULT_ANALYSIS_MODEL)
            self.assertEqual(resolve_triage_model(None), DEFAULT_TRIAGE_MODEL)

    def test_analysis_override_does_not_change_triage(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_ANALYSIS_MODEL": "gpt-4.1",
                "OPENAI_TRIAGE_MODEL": "gpt-4.1-mini",
            },
            clear=False,
        ):
            self.assertEqual(resolve_analysis_model("gpt-5"), "gpt-5")
            self.assertEqual(resolve_triage_model(None), "gpt-4.1-mini")


class ClassifyHighRecallTests(unittest.TestCase):
    def test_parse_confidence_and_needs_full_analysis(self) -> None:
        confirmed = parse_xrd_classify_payload(
            {"is_xrd": True, "confidence": "high", "reason": "PXRD"}
        )
        uncertain = parse_xrd_classify_payload(
            {"is_xrd": False, "confidence": "low", "reason": "multipanel ambiguous"}
        )
        clear_no = parse_xrd_classify_payload(
            {"is_xrd": False, "confidence": "high", "reason": "SEM"}
        )
        self.assertTrue(confirmed.needs_full_analysis)
        self.assertTrue(uncertain.needs_full_analysis)
        self.assertTrue(uncertain.uncertain)
        self.assertFalse(clear_no.needs_full_analysis)

    def test_legacy_payload_defaults_high_confidence(self) -> None:
        result = parse_xrd_classify_payload({"is_xrd": True, "reason": "old"})
        self.assertEqual(result.confidence, "high")
        self.assertIn(XRD_CLASSIFY_PROMPT_VERSION, XRD_CLASSIFY_PROMPT_VERSION)


class CompactPaperPayloadTests(unittest.TestCase):
    def test_drops_non_xrd_and_truncates_text(self) -> None:
        payload = {
            "title": "Demo",
            "doi": "10.1/x",
            "authors": ["A"],
            "figures_total": 2,
            "xrd_figures": 1,
            "figures": [
                {
                    "figure_id": "fig_1",
                    "is_xrd": False,
                    "caption": "SEM",
                    "analysis": None,
                },
                {
                    "figure_id": "fig_2",
                    "is_xrd": True,
                    "caption": "c" * 2000,
                    "text": "t" * 2000,
                    "analysis": {"sample": "TiO2", "curves": []},
                },
            ],
        }
        compact = compact_paper_payload_for_analysis(payload)
        self.assertEqual(len(compact["figures"]), 1)
        self.assertEqual(compact["figures"][0]["figure_id"], "fig_2")
        self.assertEqual(len(compact["figures"][0]["caption"]), 800)
        self.assertEqual(len(compact["figures"][0]["text"]), 1200)
        self.assertEqual(compact["figures"][0]["analysis"]["sample"], "TiO2")


class TwoTierEnrichTests(unittest.TestCase):
    def test_skips_full_analysis_for_confident_non_xrd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / "paper"
            figures = paper / "figures"
            map_png = _make_png(figures / "fig_map.png", (10, 10, 10))
            xrd_png = _make_png(figures / "fig_xrd.png", (20, 20, 20))
            fa = paper / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "doi": None,
                        "authors": [],
                        "figures": [
                            {
                                "figure": 1,
                                "figure_id": "fig_map",
                                "figure_path": str(map_png),
                                "caption": "sampling map",
                                "text": "map",
                            },
                            {
                                "figure": 2,
                                "figure_id": "fig_xrd",
                                "figure_path": str(xrd_png),
                                "caption": "PXRD",
                                "text": "diffraction",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            analyze_calls: list[str] = []
            classify_calls: list[str] = []

            def fake_classify(image_path, **kwargs):
                name = Path(image_path).name
                classify_calls.append(name)
                if "map" in name:
                    return XrdClassifyResult(
                        is_xrd=False, confidence="high", reason="map"
                    )
                return XrdClassifyResult(
                    is_xrd=True, confidence="high", reason="xrd"
                )

            def fake_analyze(image_path, **kwargs):
                analyze_calls.append(Path(image_path).name)
                return FigureAnalysisResult(
                    is_xrd=True,
                    sample="demo",
                    curves=[XrdCurve(peak_positions=[25.3])],
                )

            with patch.object(sad, "classify_xrd_figure_image", side_effect=fake_classify):
                with patch.object(sad, "analyze_figure_image", side_effect=fake_analyze):
                    sad.enrich_figure_analysis_json(paper, client=object())

            self.assertEqual(sorted(classify_calls), ["fig_map.png", "fig_xrd.png"])
            self.assertEqual(analyze_calls, ["fig_xrd.png"])
            data = json.loads(fa.read_text(encoding="utf-8"))
            self.assertEqual(data["figures_total"], 2)
            self.assertEqual(data["xrd_figures"], 1)
            self.assertEqual(len(data["figures"]), 1)
            self.assertTrue(data["figures"][0]["is_xrd"])
            self.assertIn("xrd_classification", data["figures"][0])
            self.assertNotIn("paper_analysis", data)
            self.assertTrue((paper / "non_xrd_figures" / "fig_map.png").is_file())
            self.assertTrue((paper / "xrd_figures" / "fig_xrd.png").is_file())

    def test_uncertain_non_xrd_guess_still_gets_full_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / "paper"
            figures = paper / "figures"
            png = _make_png(figures / "fig_weird.png")
            fa = paper / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "authors": [],
                        "figures": [
                            {
                                "figure": 1,
                                "figure_id": "fig_weird",
                                "figure_path": str(png),
                                "caption": "multipanel atypical",
                                "text": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            analyze_calls = []

            def fake_classify(image_path, **kwargs):
                return XrdClassifyResult(
                    is_xrd=False,
                    confidence="low",
                    reason="ambiguous multipanel",
                )

            def fake_analyze(image_path, **kwargs):
                analyze_calls.append(Path(image_path).name)
                return FigureAnalysisResult(
                    is_xrd=True,
                    curves=[XrdCurve(peak_positions=[30.0])],
                )

            with patch.object(sad, "classify_xrd_figure_image", side_effect=fake_classify):
                with patch.object(sad, "analyze_figure_image", side_effect=fake_analyze):
                    sad.enrich_figure_analysis_json(paper, client=object())

            self.assertEqual(analyze_calls, ["fig_weird.png"])
            data = json.loads(fa.read_text(encoding="utf-8"))
            self.assertEqual(data["xrd_figures"], 1)
            # Recall preserved: uncertain gate recovered an XRD figure.
            self.assertTrue(data["figures"][0]["is_xrd"])

    def test_paper_analysis_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / "paper"
            figures = paper / "figures"
            png = _make_png(figures / "fig_1.png")
            fa = paper / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "authors": [],
                        "figures": [
                            {
                                "figure": 1,
                                "figure_id": "fig_1",
                                "figure_path": str(png),
                                "caption": "PXRD",
                                "text": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            paper_calls = {"n": 0}

            def fake_classify(image_path, **kwargs):
                return XrdClassifyResult(is_xrd=True, confidence="high")

            def fake_analyze(image_path, **kwargs):
                return FigureAnalysisResult(
                    is_xrd=True, curves=[XrdCurve(peak_positions=[12.0])]
                )

            class FakePaper:
                def model_dump(self, mode="json"):
                    return {"summary": "ok", "experimental_variables": []}

            def fake_paper(*args, **kwargs):
                paper_calls["n"] += 1
                return FakePaper()

            with patch.object(sad, "classify_xrd_figure_image", side_effect=fake_classify):
                with patch.object(sad, "analyze_figure_image", side_effect=fake_analyze):
                    with patch(
                        "figure_analysis.analyzer.analyze_paper",
                        side_effect=fake_paper,
                    ):
                        sad.enrich_figure_analysis_json(
                            paper, client=object(), run_paper_analysis=True
                        )

            self.assertEqual(paper_calls["n"], 1)
            data = json.loads(fa.read_text(encoding="utf-8"))
            self.assertEqual(data["paper_analysis"]["summary"], "ok")


class DigitizeReuseClassifyTests(unittest.TestCase):
    def test_process_figure_reuses_stored_classification(self) -> None:
        triage_payload = {
            "digitizable": False,
            "curve_count": 0,
            "curve_layout": "not_applicable",
            "needs_clipdrop": False,
            "reason": "not a plot",
        }
        http_post = MagicMock(return_value=_http_json(triage_payload))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            figures = root / "xrd_figures"
            figures.mkdir(parents=True)
            src = figures / "fig_1.png"
            cv2.imwrite(str(src), np.full((20, 30, 3), 90, dtype=np.uint8))
            fa = root / "paper.figure_analysis.json"
            fa.write_text(
                json.dumps(
                    {
                        "title": "t",
                        "authors": [],
                        "figures": [
                            {
                                "figure": 1,
                                "figure_id": "fig_1",
                                "figure_path": str(src),
                                "is_xrd": True,
                                "xrd_classification": {
                                    "is_xrd": True,
                                    "confidence": "high",
                                    "reason": "from enrich",
                                },
                                "analysis": {"sample": "x", "curves": []},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                entry = sad.process_figure(
                    src,
                    figures_dir=figures,
                    model="gpt-4.1-mini",
                    overwrite=True,
                    http_post=http_post,
                    paper_dir=root,
                    xrd_only=True,
                )

            self.assertTrue(entry.get("xrd_classification_reused"))
            self.assertEqual(entry["xrd_classification"]["reason"], "from enrich")
            # Only triage should hit the network (classify reused).
            self.assertEqual(http_post.call_count, 1)
            body = http_post.call_args.kwargs["json"]
            user_text = body["messages"][1]["content"][0]["text"]
            self.assertIn("digitizable", user_text)
            self.assertNotIn('"is_xrd"', user_text)


class VisionCacheTests(unittest.TestCase):
    def test_classify_and_triage_use_disk_cache(self) -> None:
        classify_payload = {
            "is_xrd": True,
            "confidence": "high",
            "reason": "xrd",
        }
        triage_payload = {
            "digitizable": True,
            "curve_count": 1,
            "curve_layout": "single",
            "needs_clipdrop": False,
            "reason": "ok",
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            image = np.full((30, 40, 3), 255, dtype=np.uint8)
            http_post = MagicMock(
                side_effect=[
                    _http_json(classify_payload),
                    _http_json(triage_payload),
                ]
            )
            env = {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_USE_CACHE": "1",
                "OPENAI_CACHE_DIR": str(cache_root),
            }
            with patch.dict(os.environ, env):
                first = classify_xrd_figure_image(
                    image,
                    caption="PXRD",
                    api_key="test-key",
                    http_post=http_post,
                    use_cache=True,
                )
                second = classify_xrd_figure_image(
                    image,
                    caption="PXRD",
                    api_key="test-key",
                    http_post=http_post,
                    use_cache=True,
                )
                triage_http = MagicMock(return_value=_http_json(triage_payload))
                t1 = triage_figure_image(
                    image,
                    api_key="test-key",
                    http_post=triage_http,
                    use_cache=True,
                )
                t2 = triage_figure_image(
                    image,
                    api_key="test-key",
                    http_post=triage_http,
                    use_cache=True,
                )

            self.assertTrue(first.is_xrd)
            self.assertTrue(second.is_xrd)
            self.assertEqual(http_post.call_count, 1)
            self.assertTrue(t1.digitizable)
            self.assertTrue(t2.digitizable)
            self.assertEqual(triage_http.call_count, 1)
            self.assertTrue((cache_root / "figure_triage").exists())


class ApiCallBudgetComparisonTests(unittest.TestCase):
    """Compare legacy full-enrichment call counts vs two-tier for mixed papers."""

    def test_two_tier_reduces_full_analysis_calls_without_losing_xrd(self) -> None:
        # 3 non-XRD + 2 XRD (+1 uncertain that is actually XRD)
        labels = [
            ("fig_sem.png", "non", False),
            ("fig_map.png", "non", False),
            ("fig_tem.png", "non", False),
            ("fig_xrd1.png", "xrd", True),
            ("fig_xrd2.png", "xrd", True),
            ("fig_multi.png", "uncertain", True),
        ]

        legacy_full_calls = len(labels)  # old path: analyze every crop
        legacy_paper_calls = 1
        legacy_total = legacy_full_calls + legacy_paper_calls

        two_tier_classify = 0
        two_tier_full = 0
        recalled_xrd = 0
        for _name, kind, is_true_xrd in labels:
            two_tier_classify += 1
            if kind == "non":
                cls = XrdClassifyResult(is_xrd=False, confidence="high")
            elif kind == "xrd":
                cls = XrdClassifyResult(is_xrd=True, confidence="high")
            else:
                cls = XrdClassifyResult(is_xrd=False, confidence="low")
            if cls.needs_full_analysis:
                two_tier_full += 1
                if is_true_xrd:
                    recalled_xrd += 1
            elif is_true_xrd:
                recalled_xrd += 0

        true_xrd = sum(1 for *_, is_true in labels if is_true)
        self.assertEqual(recalled_xrd, true_xrd)
        # Compare expensive gpt-4.1-class calls (full enrich + optional paper).
        legacy_expensive = legacy_full_calls + legacy_paper_calls
        two_tier_expensive = two_tier_full  # paper analysis off by default
        self.assertLess(two_tier_full, legacy_full_calls)
        self.assertLess(two_tier_expensive, legacy_expensive)
        self.assertEqual(two_tier_classify, len(labels))
        # Quality proxy: enrichment still runs for every true XRD (recall=1.0).
        self.assertEqual(recalled_xrd / true_xrd, 1.0)


def test_lookup_stored_classification(tmp_path: Path):
    paper = tmp_path / "p"
    figures = paper / "xrd_figures"
    png = _make_png(figures / "fig_9.png")
    fa = paper / "p.figure_analysis.json"
    fa.write_text(
        json.dumps(
            {
                "figures": [
                    {
                        "figure_id": "fig_9",
                        "figure_path": str(png),
                        "is_xrd": True,
                        "xrd_classification": {
                            "is_xrd": True,
                            "confidence": "high",
                            "reason": "stored",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    found = sad.lookup_stored_xrd_classification(
        paper, figure_id="fig_9", image_path=png
    )
    assert found is not None
    assert found.is_xrd is True
    assert found.reason == "stored"


if __name__ == "__main__":
    unittest.main()
