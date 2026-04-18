from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

MAX_TOKENS = 8192      
MAX_DELEGATES = 8     
MAX_TURNS = 20          

def estimate_tokens(conversations: list[dict]) -> int:
    total_chars = sum(len(str(c.get("value", ""))) for c in conversations)
    return total_chars // 4


def filter_sample(sample: dict) -> tuple[bool, str | None]:
    convs = sample.get("conversations", [])
    if not convs:
        return False, "no_conversations"
    if not sample.get("gold_answer"):
        return False, "no_gold"
    est = sample.get("est_tokens") or estimate_tokens(convs)
    if est > MAX_TOKENS:
        return False, "overlong"
    n_delegates = sample.get("n_delegates", 0)
    if n_delegates > MAX_DELEGATES:
        return False, "too_many_delegates"
    if len(convs) > MAX_TURNS:
        return False, "too_many_turns"

    return True, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build SFT parquet from sft.jsonl")
    parser.add_argument("--input", required=True, help="Path to sft.jsonl from Step 1")
    parser.add_argument("--output", required=True, help="Output parquet path")
    parser.add_argument("--stats", default=None, help="Output stats JSON path (default: output.stats.json)")
    args = parser.parse_args()

    if args.stats is None:
        args.stats = args.output.replace(".parquet", "_stats.json")

    samples = []
    with open(args.input) as f:
        for line in f:
            if line.strip():
                samples.append(json.loads(line))
    print(f"Loaded {len(samples)} samples from {args.input}")
    kept = []
    drop_reasons = Counter()
    for s in samples:
        ok, reason = filter_sample(s)
        if ok:
            kept.append(s)
        else:
            drop_reasons[reason] += 1

    print(f"Kept: {len(kept)}, Dropped: {len(samples) - len(kept)}")
    if drop_reasons:
        for reason, count in drop_reasons.most_common():
            print(f"  dropped: {reason} = {count}")
    by_source = Counter(s["source"] for s in kept)
    by_domain = Counter(s["domain"] for s in kept)
    model_usage = Counter()
    skill_usage = Counter()
    total_cost = 0.0
    total_delegates = 0
    for s in kept:
        for m in s.get("models_used", []):
            model_usage[m] += 1
        for sk in s.get("skills_used", []):
            skill_usage[sk] += 1
        total_cost += s.get("total_cost", 0.0)
        total_delegates += s.get("n_delegates", 0)

    stats = {
        "n_raw": len(samples),
        "n_kept": len(kept),
        "n_dropped": len(samples) - len(kept),
        "drop_reasons": dict(drop_reasons),
        "by_source": dict(by_source),
        "by_domain": dict(by_domain),
        "model_usage": dict(model_usage.most_common()),
        "skill_usage": dict(skill_usage.most_common()),
        "total_cost_usd": round(total_cost, 4),
        "avg_delegates": round(total_delegates / max(len(kept), 1), 2),
        "avg_tokens": round(sum(s.get("est_tokens", 0) for s in kept) / max(len(kept), 1), 1),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    try:
        import pandas as pd
        df = pd.DataFrame(kept)
        df.to_parquet(args.output, index=False)
        print(f"Wrote {len(kept)} samples to {args.output}")
    except ImportError:
        fallback = args.output.replace(".parquet", ".jsonl")
        with open(fallback, "w") as f:
            for s in kept:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"pandas not available; wrote {len(kept)} samples to {fallback}")

    with open(args.stats, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"Stats: {args.stats}")

    print(f"Samples: {stats['n_kept']}")
    print(f"Sources: {stats['by_source']}")
    print(f"Avg delegates/sample: {stats['avg_delegates']}")
    print(f"Avg tokens/sample: {stats['avg_tokens']}")
    print(f"Total API cost: ${stats['total_cost_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
