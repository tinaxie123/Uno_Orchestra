"""
Merge round1 + round1_v2 trajectory files into a unified set used for the
single-stage paper narrative.

Inputs
------
data/sft/round1/trajectories/router_trajectories.jsonl
data/sft/round1/trajectories/trajectories.jsonl
data/sft/round1_v2/trajectories/router_trajectories.jsonl
data/sft/round1_v2/trajectories/trajectories.jsonl

Outputs
-------
data/sft/merged/trajectories/router_trajectories.jsonl
data/sft/merged/trajectories/trajectories.jsonl

Merge rules
-----------
Records are keyed by ``(source, idx)``.
* For duplicates, prefer round1_v2 (it was the full-pipeline run after the
  source-aware prompting convention was in place; its outcomes are canonical).
* Non-duplicates from either side are kept as-is.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path("/data/xieht/multiagentRL/data/sft")
R1 = ROOT / "round1" / "trajectories"
R2 = ROOT / "round1_v2" / "trajectories"
OUT = ROOT / "merged" / "trajectories"


def load_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def merge(name: str) -> None:
    r1 = load_jsonl(R1 / name)
    r2 = load_jsonl(R2 / name)

    merged: dict[tuple[str, int], dict] = {}
    # Insert round1 first, then let round1_v2 overwrite duplicates.
    for row in r1:
        key = (row["source"], row["idx"])
        merged[key] = row
    dup = 0
    for row in r2:
        key = (row["source"], row["idx"])
        if key in merged:
            dup += 1
        merged[key] = row

    rows = sorted(merged.values(), key=lambda x: (x["source"], x["idx"]))
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / name
    with out_path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    src_counts = Counter(r["source"] for r in rows)
    print(
        f"{name}: r1={len(r1)} r2={len(r2)} merged={len(rows)} "
        f"(duplicates overwritten by v2: {dup})"
    )
    print("  sources:", dict(src_counts))


def main() -> None:
    merge("router_trajectories.jsonl")
    merge("trajectories.jsonl")


if __name__ == "__main__":
    main()
