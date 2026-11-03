#!/usr/bin/env python3
"""Summarize all ``*.figure_analysis.json`` files under a results directory.

Examples::

    python scripts/summarize_enrichment.py
    python scripts/summarize_enrichment.py results/
    python scripts/summarize_enrichment.py results/ --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _paper_row(fa_path: Path) -> dict[str, Any]:
    payload = _load_json(fa_path) or {}
    figures = payload.get("figures") if isinstance(payload.get("figures"), list) else []
    xrd_kept = sum(1 for f in figures if isinstance(f, dict) and f.get("is_xrd"))
    with_analysis = sum(
        1
        for f in figures
        if isinstance(f, dict) and f.get("is_xrd") and isinstance(f.get("analysis"), dict)
    )
    failed = sum(
        1
        for f in figures
        if isinstance(f, dict)
        and (
            f.get("association_status") == "analysis_failed"
            or f.get("analysis_error")
        )
    )
    figures_total = payload.get("figures_total")
    if not isinstance(figures_total, int):
        figures_total = len(figures)
    xrd_figures = payload.get("xrd_figures")
    if not isinstance(xrd_figures, int):
        xrd_figures = xrd_kept

    metrics_path = fa_path.parent / "enrichment_metrics.json"
    metrics = _load_json(metrics_path) or {}

    title = str(payload.get("title") or fa_path.parent.name).strip() or fa_path.parent.name
    return {
        "paper": fa_path.parent.name,
        "title": title[:80],
        "figures_total": figures_total,
        "xrd_figures": xrd_figures,
        "xrd_with_analysis": with_analysis,
        "failed": failed,
        "api_calls": metrics.get("api_calls_total"),
        "cache_hits": metrics.get("cache_hits_total"),
        "est_cost_usd": metrics.get("estimated_cost_usd"),
        "wall_s": metrics.get("wall_seconds"),
        "path": str(fa_path),
    }


def discover_figure_analysis_jsons(root: Path) -> list[Path]:
    return sorted(root.rglob("*.figure_analysis.json"))


def summarize(root: Path) -> dict[str, Any]:
    paths = discover_figure_analysis_jsons(root)
    papers = [_paper_row(p) for p in paths]

    def _sum(key: str) -> float | int:
        vals = [r[key] for r in papers if isinstance(r.get(key), (int, float))]
        return sum(vals) if vals else 0

    n = len(papers)
    est_cost = float(_sum("est_cost_usd"))
    wall_s = float(_sum("wall_s"))
    totals = {
        "papers": n,
        "figures_total": int(_sum("figures_total")),
        "xrd_figures": int(_sum("xrd_figures")),
        "xrd_with_analysis": int(_sum("xrd_with_analysis")),
        "failed": int(_sum("failed")),
        "api_calls": int(_sum("api_calls")),
        "cache_hits": int(_sum("cache_hits")),
        "est_cost_usd": round(est_cost, 4),
        "wall_s": round(wall_s, 1),
        "avg_cost_usd_per_pdf": round(est_cost / n, 4) if n else 0.0,
        "avg_wall_s_per_pdf": round(wall_s / n, 1) if n else 0.0,
    }
    return {"root": str(root.resolve()), "papers": papers, "totals": totals}


def _fmt_num(value: Any, *, width: int = 6, decimals: int | None = None) -> str:
    if value is None:
        return "-".rjust(width)
    if decimals is not None:
        try:
            return f"{float(value):.{decimals}f}".rjust(width)
        except (TypeError, ValueError):
            return "-".rjust(width)
    try:
        return str(int(value)).rjust(width)
    except (TypeError, ValueError):
        return "-".rjust(width)


def print_table(summary: dict[str, Any]) -> None:
    papers = summary["papers"]
    totals = summary["totals"]
    if not papers:
        print(f"No *.figure_analysis.json under {summary['root']}")
        return

    header = (
        f"{'figs':>4} {'xrd':>4} {'anal':>4} {'fail':>4} "
        f"{'api':>5} {'cache':>5} {'$est':>7} {'wall':>6}  paper"
    )
    print(f"Enrichment summary for {summary['root']}")
    print(f"Papers: {totals['papers']}")
    print(header)
    print("-" * len(header))
    for row in papers:
        print(
            f"{_fmt_num(row['figures_total'], width=4)} "
            f"{_fmt_num(row['xrd_figures'], width=4)} "
            f"{_fmt_num(row['xrd_with_analysis'], width=4)} "
            f"{_fmt_num(row['failed'], width=4)} "
            f"{_fmt_num(row['api_calls'], width=5)} "
            f"{_fmt_num(row['cache_hits'], width=5)} "
            f"{_fmt_num(row['est_cost_usd'], width=7, decimals=3)} "
            f"{_fmt_num(row['wall_s'], width=6, decimals=1)}  "
            f"{row['paper'][:70]}"
        )
    print("-" * len(header))
    print(
        f"{_fmt_num(totals['figures_total'], width=4)} "
        f"{_fmt_num(totals['xrd_figures'], width=4)} "
        f"{_fmt_num(totals['xrd_with_analysis'], width=4)} "
        f"{_fmt_num(totals['failed'], width=4)} "
        f"{_fmt_num(totals['api_calls'], width=5)} "
        f"{_fmt_num(totals['cache_hits'], width=5)} "
        f"{_fmt_num(totals['est_cost_usd'], width=7, decimals=3)} "
        f"{_fmt_num(totals['wall_s'], width=6, decimals=1)}  "
        f"TOTAL"
    )
    extras: list[str] = []
    if totals["figures_total"]:
        pct = 100.0 * totals["xrd_figures"] / totals["figures_total"]
        extras.append(
            f"XRD yield: {totals['xrd_figures']}/{totals['figures_total']} ({pct:.1f}%)"
        )
    if totals["papers"]:
        extras.append(
            f"Avg per PDF: ${totals['avg_cost_usd_per_pdf']:.4f}, "
            f"{totals['avg_wall_s_per_pdf']:.1f}s wall"
        )
    if extras:
        print("\n" + " | ".join(extras))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results_dir",
        nargs="?",
        type=Path,
        default=Path("results"),
        help="Directory containing per-paper enrichment outputs (default: results/)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of a table",
    )
    args = parser.parse_args(argv)

    root = args.results_dir.resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    summary = summarize(root)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print_table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
