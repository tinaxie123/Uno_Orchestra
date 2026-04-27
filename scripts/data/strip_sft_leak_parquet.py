"""Apply the strip_sft_leak human-turn cleaner to the HF release parquet.

The HF release artefact at
``data/sft/hf_release/sft_full/train.parquet`` carries the same five-marker
human-prompt leak as ``router_sft.json`` (verified 2026-04-27: 95.5%
``Correct answer`` / 45.2% ``REAL EVIDENCE`` / 95.5% ``BEHAVIORAL HINT``
hits, byte-identical hit counts to the JSON cleaner's stats sidecar).

This script reuses ``strip_sft_leak.clean_human`` row-wise so the parquet
is byte-aligned with ``router_sft_clean.json`` after cleaning. System /
gpt / observation turns pass through unchanged.

Output: sibling parquet ``train.parquet`` next to the input, plus a
``train.stats.json`` sidecar with marker-hit counts.

Usage
-----
    python scripts/data/strip_sft_leak_parquet.py \\
        --in  data/sft/hf_release/sft_full/train.parquet \\
        --out data/sft/hf_release/sft_full_clean/train.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# Reuse the JSON cleaner's marker logic verbatim — single source of truth.
sys.path.insert(0, str(Path(__file__).parent))
from strip_sft_leak import clean_human  # noqa: E402


def process_conversations(
    convs: list,
    *,
    strip_answer: bool,
    strip_evidence: bool,
    strip_hint: bool,
    tally: Counter,
) -> tuple[list, bool]:
    """Return (new conversations list, changed)."""
    changed = False
    new_convs = []
    for c in convs:
        if c.get("from") != "human":
            new_convs.append(c)
            continue
        old = c.get("value", "")
        new, hit = clean_human(
            old,
            strip_answer=strip_answer,
            strip_evidence=strip_evidence,
            strip_hint=strip_hint,
        )
        if new != old:
            changed = True
            for h in hit:
                tally[f"strip_{h}"] += 1
            new_convs.append({**c, "value": new})
        else:
            new_convs.append(c)
    return new_convs, changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--in", dest="inp", required=True,
                    help="Path to leaked train.parquet (input).")
    ap.add_argument("--out", required=True,
                    help="Path to clean train.parquet (output).")
    ap.add_argument("--stats", default=None,
                    help="Stats JSON path (default: <out>.stats.json).")
    ap.add_argument("--strip-answer", dest="strip_answer",
                    action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--strip-evidence", dest="strip_evidence",
                    action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--strip-hint", dest="strip_hint",
                    action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if out.exists() and not args.overwrite:
        print(f"refusing to overwrite {out} (pass --overwrite)", file=sys.stderr)
        return 2

    print(f"[load] {inp} ({inp.stat().st_size / 1e6:.1f} MB)")
    df = pd.read_parquet(inp)
    print(f"  rows: {len(df)}, columns: {list(df.columns)}")
    print(f"[flags] strip_answer={args.strip_answer}  "
          f"strip_evidence={args.strip_evidence}  "
          f"strip_hint={args.strip_hint}")

    tally: Counter[str] = Counter()
    n_changed = 0
    new_convs_col = []
    for convs in df["conversations"]:
        new_convs, changed = process_conversations(
            list(convs),
            strip_answer=args.strip_answer,
            strip_evidence=args.strip_evidence,
            strip_hint=args.strip_hint,
            tally=tally,
        )
        new_convs_col.append(new_convs)
        if changed:
            n_changed += 1
    df = df.assign(conversations=new_convs_col)

    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[write] {out}")
    df.to_parquet(out, engine="pyarrow", index=False)
    print(f"  wrote {len(df)} rows ({out.stat().st_size / 1e6:.1f} MB), "
          f"modified {n_changed}")

    stats_path = Path(args.stats) if args.stats else out.with_suffix(".stats.json")
    stats = {
        "input": str(inp),
        "output": str(out),
        "n_records": int(len(df)),
        "n_records_modified": n_changed,
        "flags": {
            "strip_answer": args.strip_answer,
            "strip_evidence": args.strip_evidence,
            "strip_hint": args.strip_hint,
        },
        "marker_hits": dict(tally),
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"[stats] {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
