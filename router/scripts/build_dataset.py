"""
Build a final SFT training parquet from one or more raw distill jsonl files.

Implements data/sft_protocol.md:
- Re-validates every sample against current schema + pools.yaml
- Applies the §2 filter rules
- Computes repair_type via the §5.1 classifier
- Computes behavior_match
- Writes data/sft/train_{snapshot}.parquet + train_{snapshot}_stats.json
- Appends a snapshot manifest line to data/sft/snapshots.jsonl

Usage
-----
    python3 scripts/build_dataset.py --inputs data/sft/pilot400.jsonl --snapshot pilot400_v1
    python3 scripts/build_dataset.py --inputs data/sft/pilot400.jsonl data/sft/dryrun30v3.jsonl --snapshot mixed_v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_schema import (  # noqa: E402
    PLAN_RE,
    SUBTASK_RE,
    load_pools,
    validate_messages,
)

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
POOLS_PATH = os.path.join(REPO_ROOT, "config/pools.yaml")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "data/sft")
SNAPSHOTS_MANIFEST = os.path.join(DEFAULT_OUT_DIR, "snapshots.jsonl")

# Filter thresholds (from sft_protocol.md §2)
MAX_ATTEMPTS = 3
MAX_OBS_LEN = 4096
MAX_TOTAL_TOKENS_EST = 16384
MAX_ROUTES = 8
MAX_PLAN_ROUNDS = 3


# ---------------------------------------------------------------------------
# repair_type classifier (§5.1)
# ---------------------------------------------------------------------------


def parse_subtasks_by_round(messages: list[dict]) -> dict[int, list[tuple[int, list[int]]]]:
    """Return {round: [(subtask_id, depends_on_list), ...]}."""
    out: dict[int, list[tuple[int, list[int]]]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        for plan_match in PLAN_RE.finditer(content):
            round_num = int(plan_match.group(1))
            inner = plan_match.group(2)
            for st_match in SUBTASK_RE.finditer(inner):
                sid = int(st_match.group(1))
                deps_str = st_match.group(2).strip()
                deps = [int(x) for x in deps_str.split(",")] if deps_str else []
                out.setdefault(round_num, []).append((sid, deps))
    return out


VERIFY_RE_LOCAL = re.compile(
    r'<verify[^>]*status="repair_needed"[^>]*>(.*?)</verify>',
    re.DOTALL,
)

# Keyword heuristics for the verify reason text
CONTINUATION_KEYWORDS = (
    "unblock", "unblocks", "next phase", "next step", "now we can", "now that we",
    "becomes expressible", "reveals", "revealed", "identified", "identifies",
    "narrowed", "now know", "can now", "specify the exact", "having found",
    "with this", "based on this", "given the", "given that we now",
    "follow-up", "follow up", "next round can", "enables",
)
DECOMP_REPAIR_KEYWORDS = (
    "wrong subtask", "structurally", "missing key", "wrong dependency",
    "wrong granularity", "mistake in the plan", "incorrect plan",
    "restructure", "wrongly decomposed", "should have been split",
    "wrong split", "wrong decomposition", "plan was wrong",
    "miscategorized", "wrong order",
)


def classify_repair_type(messages: list[dict], n_plan_rounds: int) -> str:
    """Heuristic classifier per sft_protocol.md §5.1.

    Multi-round trajectories are classified by INSPECTING THE VERIFY REASON
    text (not just the DAG structure), because well-formed continuation samples
    naturally carry depends_on links and would otherwise be misclassified.
    """
    if n_plan_rounds == 0:
        return "lazy"
    if n_plan_rounds == 1:
        return "oneshot"

    # Collect every <verify status="repair_needed"> reason text in the trajectory
    verify_texts = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for vm in VERIFY_RE_LOCAL.finditer(msg.get("content", "")):
            verify_texts.append(vm.group(1).lower())
    combined = " ".join(verify_texts)

    has_cont_kw = any(kw in combined for kw in CONTINUATION_KEYWORDS)
    has_repair_kw = any(kw in combined for kw in DECOMP_REPAIR_KEYWORDS)

    # Strong signals first
    if has_repair_kw and not has_cont_kw:
        return "decomp_repair"
    if has_cont_kw and not has_repair_kw:
        return "continuation"

    # Both or neither: fall back to structural heuristic
    by_round = parse_subtasks_by_round(messages)
    round_1_ids = {sid for sid, _ in by_round.get(1, [])}
    later_ids: set[int] = set()
    for r, items in by_round.items():
        if r <= 1:
            continue
        for sid, _ in items:
            later_ids.add(sid)

    if later_ids and (later_ids & round_1_ids):
        # Later rounds REUSE round-1 ids → genuine execution-style retry
        return "execution_repair"

    # Default: assume continuation when in doubt (matches our prompt's
    # heavy bias toward observation-driven continuation over decomp_repair).
    return "continuation"


# ---------------------------------------------------------------------------
# Filtering (§2)
# ---------------------------------------------------------------------------


OBS_RE_SIMPLE = re.compile(r"<obs[^>]*>(.*?)</obs>", re.DOTALL)


def estimate_tokens(messages: list[dict]) -> int:
    total_chars = sum(len(m.get("content", "")) for m in messages)
    return total_chars // 4  # rough


def filter_sample(raw: dict, pools: dict) -> tuple[bool, str | None]:
    """Return (kept, drop_reason)."""
    if not raw.get("valid", False):
        return False, "raw_not_valid"
    messages = raw.get("messages", [])
    if not messages:
        return False, "no_messages"

    # Re-validate against current schema + pools
    result = validate_messages(messages, pools=pools)
    if not result.valid:
        return False, f"revalidation_fail:{result.errors[0].code if result.errors else 'unknown'}"

    if raw.get("n_attempts", 0) > MAX_ATTEMPTS:
        return False, "max_attempts_exceeded"
    if not raw.get("gold"):
        return False, "no_gold"

    stats = raw.get("stats", {})
    if stats.get("n_routes", 0) > MAX_ROUTES:
        return False, "too_many_routes"
    if stats.get("n_plan_rounds", 0) > MAX_PLAN_ROUNDS:
        return False, "too_many_plan_rounds"

    # obs length
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        for obs_match in OBS_RE_SIMPLE.finditer(msg.get("content", "")):
            if len(obs_match.group(1)) > MAX_OBS_LEN:
                return False, "obs_too_long"

    if estimate_tokens(messages) > MAX_TOTAL_TOKENS_EST:
        return False, "token_budget_exceeded"

    return True, None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build(input_paths: list[str], snapshot_id: str, out_dir: str = DEFAULT_OUT_DIR) -> dict:
    pools = load_pools(POOLS_PATH)
    rows: list[dict] = []
    dropped_rows: list[dict] = []
    by_repair_type = Counter()
    by_behavior = Counter()
    by_domain = Counter()
    by_model = Counter()
    by_skill = Counter()
    behavior_match_count = 0
    n_raw = 0
    cost_total = 0.0
    attempts_total = 0
    tokens_total = 0
    msgs_total = 0

    for input_path in input_paths:
        if not os.path.exists(input_path):
            print(f"  ! input not found: {input_path}", file=sys.stderr)
            continue
        with open(input_path) as f:
            for line in f:
                raw = json.loads(line)
                n_raw += 1
                kept, reason = filter_sample(raw, pools)
                if not kept:
                    dropped_rows.append({**raw, "drop_reason": reason})
                    continue
                # Compute repair_type
                stats = raw.get("stats", {})
                n_plan_rounds = stats.get("n_plan_rounds", 0)
                repair_type = classify_repair_type(raw["messages"], n_plan_rounds)
                behavior = raw.get("behavior", "unknown")
                behavior_match = (behavior == repair_type)
                if behavior_match:
                    behavior_match_count += 1

                # Aggregate stats
                by_repair_type[repair_type] += 1
                by_behavior[behavior] += 1
                by_domain[raw.get("domain", "unknown")] += 1
                for m in stats.get("models_used", []):
                    by_model[m] += 1
                for s in stats.get("skills_used", []):
                    by_skill[s] += 1
                cost_total += raw.get("cost_usd", 0.0)
                attempts_total += raw.get("n_attempts", 0)
                tokens_total += estimate_tokens(raw["messages"])
                msgs_total += len(raw["messages"])

                # Build row matching protocol §6
                row = {
                    "messages": raw["messages"],
                    "id": raw["id"],
                    "source": raw.get("source", ""),
                    "domain": raw.get("domain", ""),
                    "behavior": behavior,
                    "repair_type": repair_type,
                    "behavior_match": behavior_match,
                    "n_plan_rounds": n_plan_rounds,
                    "n_routes": stats.get("n_routes", 0),
                    "n_subtasks": stats.get("n_subtasks", 0),
                    "models_used": list(stats.get("models_used", [])),
                    "skills_used": list(stats.get("skills_used", [])),
                    "gold": raw.get("gold", ""),
                    "teacher": raw.get("teacher", ""),
                    "n_attempts": raw.get("n_attempts", 0),
                    "cost_usd": raw.get("cost_usd", 0.0),
                    "input_tokens": raw.get("input_tokens", 0),
                    "output_tokens": raw.get("output_tokens", 0),
                }
                rows.append(row)

    n_kept = len(rows)
    drop_reasons = Counter(r.get("drop_reason", "unknown") for r in dropped_rows)

    # Compute distill_prompt_sha256 from current generate_trajectories.py source.
    distill_path = os.path.join(REPO_ROOT, "scripts/generate_trajectories.py")
    if os.path.exists(distill_path):
        with open(distill_path) as f:
            distill_sha = hash_text(f.read())
    else:
        distill_sha = "unknown"

    stats_obj = {
        "snapshot_id": snapshot_id,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_files": [os.path.relpath(p, REPO_ROOT) for p in input_paths],
        "schema_version": "final",
        "pools_version": "final",
        "distill_prompt_sha256": distill_sha,
        "filter_rules_version": "final",
        "n_raw": n_raw,
        "n_kept": n_kept,
        "n_dropped": len(dropped_rows),
        "drop_reasons": dict(drop_reasons),
        "by_repair_type": dict(by_repair_type),
        "by_behavior": dict(by_behavior),
        "behavior_match_rate": (behavior_match_count / n_kept) if n_kept else 0.0,
        "by_domain": dict(by_domain),
        "by_model": dict(by_model),
        "by_skill": dict(by_skill),
        "total_cost_usd": round(cost_total, 4),
        "avg_attempts": round(attempts_total / n_kept, 3) if n_kept else 0.0,
        "avg_messages_per_sample": round(msgs_total / n_kept, 2) if n_kept else 0.0,
        "avg_total_tokens_per_sample": round(tokens_total / n_kept, 1) if n_kept else 0.0,
    }

    # Write outputs
    os.makedirs(out_dir, exist_ok=True)
    parquet_path = os.path.join(out_dir, f"train_{snapshot_id}.parquet")
    stats_path = os.path.join(out_dir, f"train_{snapshot_id}_stats.json")
    dropped_path = os.path.join(out_dir, f"train_{snapshot_id}_dropped.jsonl")

    # Write parquet
    try:
        import pandas as pd  # noqa: WPS433

        df = pd.DataFrame(rows)
        df.to_parquet(parquet_path, index=False)
    except ImportError:
        # Fallback: write jsonl if pandas/pyarrow not available
        parquet_path = parquet_path.replace(".parquet", ".jsonl")
        with open(parquet_path, "w") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  ! pandas not available; wrote jsonl instead of parquet")

    # Write stats
    with open(stats_path, "w") as f:
        json.dump(stats_obj, f, indent=2, ensure_ascii=False)

    # Write dropped samples
    if dropped_rows:
        with open(dropped_path, "w") as f:
            for r in dropped_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Append to snapshot manifest
    with open(SNAPSHOTS_MANIFEST, "a") as f:
        f.write(json.dumps({k: v for k, v in stats_obj.items() if k != "by_domain"}, ensure_ascii=False) + "\n")

    return stats_obj


# ---------------------------------------------------------------------------
# Validation gates (§8)
# ---------------------------------------------------------------------------


def check_gates(stats: dict, mode: str = "pilot") -> tuple[bool, list[str]]:
    """Return (pass, list_of_failures)."""
    failures = []
    behavior_match = stats["behavior_match_rate"]
    if behavior_match < 0.90:
        failures.append(f"behavior_match_rate={behavior_match:.2%} < 0.90")

    n_kept = stats["n_kept"]
    n_raw = stats["n_raw"]
    drop_rate = (stats["n_dropped"] / n_raw) if n_raw else 0.0
    if drop_rate > 0.05:
        failures.append(f"drop_rate={drop_rate:.2%} > 5%")

    min_per_domain = 50 if mode == "full" else 5
    for domain, count in stats["by_domain"].items():
        if count < min_per_domain:
            failures.append(f"domain {domain} has {count} samples < {min_per_domain}")

    min_per_repair_type = 50 if mode == "full" else 5
    for rt, count in stats["by_repair_type"].items():
        if count < min_per_repair_type:
            failures.append(f"repair_type {rt} has {count} samples < {min_per_repair_type}")

    return len(failures) == 0, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True, help="raw distill jsonl file(s)")
    parser.add_argument("--snapshot", required=True, help="snapshot id, e.g. pilot400_v1")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--mode", default="pilot", choices=["pilot", "full"], help="affects validation gates in §8")
    args = parser.parse_args()

    print(f"building snapshot '{args.snapshot}' from {args.inputs}")
    stats = build(args.inputs, args.snapshot, args.out_dir)

    print()
    print("=" * 78)
    print(f"snapshot: {stats['snapshot_id']}")
    print(f"raw: {stats['n_raw']}  kept: {stats['n_kept']}  dropped: {stats['n_dropped']}")
    print(f"drop_reasons: {stats['drop_reasons']}")
    print(f"behavior_match_rate: {stats['behavior_match_rate']:.2%}")
    print(f"by_repair_type: {stats['by_repair_type']}")
    print(f"by_behavior:    {stats['by_behavior']}")
    print(f"by_domain ({len(stats['by_domain'])}): {stats['by_domain']}")
    print(f"avg attempts: {stats['avg_attempts']}  avg tokens/sample: {stats['avg_total_tokens_per_sample']}")
    print(f"total cost: ${stats['total_cost_usd']}")
    print()

    ok, failures = check_gates(stats, mode=args.mode)
    if ok:
        print(f"VALIDATION GATES ({args.mode}): ✅ PASS")
        return 0
    print(f"VALIDATION GATES ({args.mode}): ❌ FAIL")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
