"""
Merge round1 + round1_v2 SFT parquets into a single unified training set.

Rules
-----
1. Start from the union of `question` strings across both rounds.
2. For a duplicate question, prefer the row whose `strategy == 'direct'`
   (the lazy-mode demonstration is the scarce signal; we keep it).
3. Otherwise prefer round1_v2 (ToolACE fix applied, more recent).
4. Log the per-source/per-domain distribution of the merged set.

Output
------
data/sft/merged/sft.parquet        — union, deduped
data/sft/merged/merge_report.json  — counts for the write-up
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path("/data/xieht/multiagentRL/data/sft")
R1 = ROOT / "round1" / "sft.parquet"
R2 = ROOT / "round1_v2" / "sft.parquet"
OUT_DIR = ROOT / "merged"
OUT_PARQUET = OUT_DIR / "sft.parquet"
OUT_REPORT = OUT_DIR / "merge_report.json"


def strategy_rank(strategy: str) -> int:
    # lower is better when resolving duplicates
    return {"direct": 0}.get(strategy, 1)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    df1 = pd.read_parquet(R1)
    df2 = pd.read_parquet(R2)

    # Align columns — round1 and round1_v2 share the same schema already.
    common_cols = [c for c in df1.columns if c in df2.columns]
    df1 = df1[common_cols].copy()
    df2 = df2[common_cols].copy()
    df1["_round"] = "round1"
    df2["_round"] = "round1_v2"

    combined = pd.concat([df1, df2], ignore_index=True)

    # Deduplicate by question. Sort so that preferred rows come first.
    combined["_strategy_rank"] = combined["strategy"].map(strategy_rank)
    combined["_round_rank"] = (combined["_round"] != "round1_v2").astype(int)
    combined = combined.sort_values(["_strategy_rank", "_round_rank"], kind="mergesort")
    combined = combined.drop_duplicates("question", keep="first").reset_index(drop=True)
    combined = combined.drop(columns=["_strategy_rank", "_round_rank"])

    report = {
        "round1_rows": len(df1),
        "round1_v2_rows": len(df2),
        "merged_rows": len(combined),
        "per_source_round": {
            "round1": dict(Counter(df1["source"])),
            "round1_v2": dict(Counter(df2["source"])),
        },
        "merged_source_counts": dict(Counter(combined["source"])),
        "merged_domain_counts": dict(Counter(combined["domain"])),
        "merged_strategy_counts": dict(Counter(combined["strategy"])),
        "kept_from_round1": int((combined["_round"] == "round1").sum()),
        "kept_from_round1_v2": int((combined["_round"] == "round1_v2").sum()),
    }

    combined.to_parquet(OUT_PARQUET, index=False)
    OUT_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"wrote {OUT_PARQUET}: {len(combined)} rows")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
