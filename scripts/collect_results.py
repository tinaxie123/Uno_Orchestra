#!/usr/bin/env python3
"""Collect paper-style evaluation summaries.

Reads ``<root>/<model>/<benchmark>/summary.json`` and reports:
- per-benchmark pass@1/pass@2
- 13-benchmark macro average
- 5-domain macro average
- average context tokens, output tokens, and USD/query

Missing benchmark runs are reported explicitly and excluded from averages.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean


PAPER_BENCHMARKS = (
    "gpqa",
    "mmlu",
    "math500",
    "aime",
    "drop",
    "humaneval",
    "mbpp",
    "gaia",
    "livecodebench",
    "toolbench",
    "mrcr",
    "swebench",
    "terminalbench",
)


BENCHMARK_DOMAINS = {
    "gpqa": "knowledge",
    "mmlu": "knowledge",
    "drop": "knowledge",
    "math500": "math",
    "aime": "math",
    "humaneval": "code",
    "mbpp": "code",
    "livecodebench": "code",
    "swebench": "agentic",
    "terminalbench": "agentic",
    "gaia": "agentic",
    "toolbench": "agentic",
    "mrcr": "long_context",
}


DOMAIN_ORDER = (
    "knowledge",
    "math",
    "code",
    "agentic",
    "long_context",
)


def _bench_key(path_name: str) -> str:
    name = path_name.lower()
    aliases = {
        "math-500": "math500",
        "aime-2025": "aime",
        "swe-bench_verified": "swebench",
        "terminal-bench-2.0": "terminalbench",
        "livecodebench-v6": "livecodebench",
        "mrcr-v2": "mrcr",
    }
    return aliases.get(name, name)


def _load(root: Path) -> dict[str, dict[str, dict]]:
    results: dict[str, dict[str, dict]] = {}
    for summary in root.glob("*/*/summary.json"):
        model = summary.parent.parent.name
        bench = _bench_key(summary.parent.name)
        try:
            data = json.loads(summary.read_text())
        except json.JSONDecodeError:
            continue
        results.setdefault(model, {})[bench] = data
    return results


def _avg(values: list[float]) -> float | None:
    return mean(values) if values else None


def _summarize_model(model: str, runs: dict[str, dict]) -> dict:
    present = [b for b in PAPER_BENCHMARKS if b in runs]
    missing = [b for b in PAPER_BENCHMARKS if b not in runs]

    domain_scores: dict[str, dict[str, float | None]] = {}
    for domain in DOMAIN_ORDER:
        domain_benches = [b for b, d in BENCHMARK_DOMAINS.items() if d == domain and b in runs]
        domain_scores[domain] = {
            "pass_at_1": _avg([float(runs[b].get("pass_at_1", 0.0)) for b in domain_benches]),
            "pass_at_2": _avg([float(runs[b].get("pass_at_2", runs[b].get("pass_at_1", 0.0))) for b in domain_benches]),
        }

    domain_p1 = [v["pass_at_1"] for v in domain_scores.values() if v["pass_at_1"] is not None]
    domain_p2 = [v["pass_at_2"] for v in domain_scores.values() if v["pass_at_2"] is not None]

    mode_counts: dict[str, int] = {}
    for bench in present:
        mode = str(runs[bench].get("scoring_mode", "official_compatible"))
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

    return {
        "model": model,
        "completed": len(present),
        "missing": missing,
        "scoring_modes": mode_counts,
        "macro13_pass_at_1": _avg([float(runs[b].get("pass_at_1", 0.0)) for b in present]),
        "macro13_pass_at_2": _avg([float(runs[b].get("pass_at_2", runs[b].get("pass_at_1", 0.0))) for b in present]),
        "domain_macro_pass_at_1": _avg([float(x) for x in domain_p1]),
        "domain_macro_pass_at_2": _avg([float(x) for x in domain_p2]),
        "avg_cost_usd_per_query": _avg([float(runs[b].get("avg_cost_usd_per_query", runs[b].get("avg_cost", 0.0))) for b in present]),
        "avg_context_tokens": _avg([float(runs[b].get("avg_context_tokens", 0.0)) for b in present]),
        "avg_output_tokens": _avg([float(runs[b].get("avg_output_tokens", 0.0)) for b in present]),
        "domains": domain_scores,
        "benchmarks": {b: runs[b] for b in present},
    }


def _fmt_pct(x: float | None) -> str:
    return "NA" if x is None else f"{100.0 * x:.1f}"


def _fmt_num(x: float | None, places: int = 4) -> str:
    return "NA" if x is None else f"{x:.{places}f}"


def _emit_md(rows: list[dict]) -> None:
    headers = [
        "model", "done", "macro13 p@1", "macro13 p@2",
        "domain p@1", "domain p@2", "score modes", "USD/q", "ctx tok", "out tok", "missing",
    ]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for r in rows:
        print(
            "| "
            + " | ".join([
                r["model"],
                f"{r['completed']}/{len(PAPER_BENCHMARKS)}",
                _fmt_pct(r["macro13_pass_at_1"]),
                _fmt_pct(r["macro13_pass_at_2"]),
                _fmt_pct(r["domain_macro_pass_at_1"]),
                _fmt_pct(r["domain_macro_pass_at_2"]),
                ",".join(f"{k}:{v}" for k, v in sorted(r["scoring_modes"].items())) or "-",
                _fmt_num(r["avg_cost_usd_per_query"], 6),
                _fmt_num(r["avg_context_tokens"], 1),
                _fmt_num(r["avg_output_tokens"], 1),
                ",".join(r["missing"]) if r["missing"] else "-",
            ])
            + " |"
        )


def _emit_csv(rows: list[dict]) -> None:
    fieldnames = [
        "model", "completed", "macro13_pass_at_1", "macro13_pass_at_2",
        "domain_macro_pass_at_1", "domain_macro_pass_at_2",
        "scoring_modes", "avg_cost_usd_per_query", "avg_context_tokens", "avg_output_tokens", "missing",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({
            k: (
                ",".join(r[k]) if k == "missing"
                else json.dumps(r[k], sort_keys=True) if k == "scoring_modes"
                else r[k]
            )
            for k in fieldnames
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data/eval", help="Evaluation output root")
    parser.add_argument("--format", choices=["md", "json", "csv"], default="md")
    args = parser.parse_args()

    root = Path(args.root)
    rows = [_summarize_model(model, runs) for model, runs in sorted(_load(root).items())]
    if args.format == "json":
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif args.format == "csv":
        _emit_csv(rows)
    else:
        _emit_md(rows)


if __name__ == "__main__":
    main()
