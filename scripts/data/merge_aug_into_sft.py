"""
Merge successful rejection-sampled trajectories into the existing SFT parquet.

Reads:
  - data/sft/merged/sft.parquet              — current SFT set (2,762 rows)
  - data/sft/merged/sft_aug.jsonl            — rollouts from augment_sft.py

Steps:
  1. Filter aug rollouts to those with teacher_ok=True.
  2. Convert each aug record into the SFT parquet schema (same columns as the
     existing sft.parquet): source / domain / prompt_type / question /
     gold_answer / strategy / n_delegates / final_answer / delegations /
     models_used / skills_used / conversations_raw.
  3. Deduplicate: for duplicate (question, final_answer) pairs prefer the
     existing SFT row; otherwise keep the aug row only if the *first* row for
     that question — keeps diversity but avoids near-identical re-samples of
     the same strategy/answer.
  4. Write data/sft/merged/sft_v2.parquet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.data.formatter import to_sharegpt, estimate_tokens

SFT_PARQUET = ROOT / "data/sft/merged/sft.parquet"
AUG_JSONL = ROOT / "data/sft/merged/sft_aug.jsonl"
OUT_PARQUET = ROOT / "data/sft/merged/sft_v2.parquet"

MAX_TRAINING_TOKENS = 8192


def _pack_aug(rec: dict) -> dict | None:
    """Convert a single aug rollout into the SFT-parquet schema."""
    if not rec.get("teacher_ok"):
        return None
    traj = rec.get("trajectory") or {}
    msgs = traj.get("messages") or []
    if not msgs:
        return None
    if estimate_tokens(msgs) > MAX_TRAINING_TOKENS:
        return None

    # Reproduce the 'strategy' label that sft.parquet uses
    n_delegates = int(traj.get("n_delegates") or 0)
    if n_delegates == 0:
        strategy = "direct"
    elif n_delegates == 1:
        strategy = "single"
    else:
        strategy = "multi"

    conversations = to_sharegpt(msgs)

    return {
        "source": str(rec["source"]),
        "domain": str(rec.get("domain") or ""),
        "prompt_type": "planner_default",  # matches existing schema
        "question": str(rec["question"]),
        "gold_answer": str(rec["gold_answer"]),
        "strategy": strategy,
        "n_delegates": n_delegates,
        "final_answer": str(traj.get("answer") or ""),
        "delegations": json.dumps(traj.get("routing_decisions") or [], ensure_ascii=False),
        "models_used": json.dumps(traj.get("models_used") or [], ensure_ascii=False),
        "skills_used": json.dumps(traj.get("skills_used") or [], ensure_ascii=False),
        "conversations_raw": json.dumps(conversations, ensure_ascii=False),
    }


def main() -> None:
    print(f"[load] SFT parquet: {SFT_PARQUET}")
    sft_df = pd.read_parquet(SFT_PARQUET)
    print(f"  rows: {len(sft_df)}")

    print(f"[load] Aug JSONL: {AUG_JSONL}")
    aug_rows: list[dict] = []
    n_total = n_ok = n_packed = 0
    with AUG_JSONL.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            n_total += 1
            if not d.get("teacher_ok"):
                continue
            n_ok += 1
            packed = _pack_aug(d)
            if packed is None:
                continue
            n_packed += 1
            aug_rows.append(packed)
    print(f"  rollouts: {n_total}  teacher_ok: {n_ok}  survived packing: {n_packed}")

    if not aug_rows:
        print("[merge] no new aug rows to add, writing copy of existing SFT → sft_v2")
        sft_df.to_parquet(OUT_PARQUET, index=False)
        return

    # Concatenate and dedup on (question, final_answer)
    aug_df = pd.DataFrame(aug_rows)
    # Align columns
    aug_df = aug_df.reindex(columns=sft_df.columns, fill_value="")
    combined = pd.concat([sft_df, aug_df], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["question", "final_answer"], keep="first")
    after = len(combined)
    kept_new = after - len(sft_df)
    print(f"[merge] combined rows: {before} → after dedup: {after}")
    print(f"        net new added: {kept_new}")

    combined.to_parquet(OUT_PARQUET, index=False)
    print(f"[write] {OUT_PARQUET}: {len(combined)} rows")

    # Print distribution shift
    print("\nsource distribution (before → after):")
    before_counts = sft_df["source"].value_counts()
    after_counts = combined["source"].value_counts()
    all_src = sorted(set(before_counts.index) | set(after_counts.index))
    for s in all_src:
        b = before_counts.get(s, 0)
        a = after_counts.get(s, 0)
        print(f"  {s:<12}  {b:>5} → {a:>5}  (+{a-b})")


if __name__ == "__main__":
    main()
