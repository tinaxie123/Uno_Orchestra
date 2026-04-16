"""Bootstrapped curriculum filtering pipeline.

Stage 1: Router probe — run current router on each task, discard if pass@3
Stage 2: Teacher run — for failed tasks, collect teacher trajectory
Stage 3: Overlong filter — discard trajectories exceeding token budget

Outputs:
    sft.jsonl  — teacher-correct trajectories (SFT training)
    rl.jsonl   — teacher-also-failed tasks (RL training)
    curriculum_report.json — statistics
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict

from tqdm import tqdm

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.insert(0, REPO_ROOT)

from configs import PoolConfig, load_pools
from scripts.data.agent import run_agent
from scripts.data.data_loader import load_recipe, load_question_pool
from scripts.data.formatter import filter_and_pack
from scripts.data.verifiers import verify

RECIPE_PATH = os.path.join(REPO_ROOT, "configs/sft/data/sft_recipe.yaml")
POOLS_PATH = os.path.join(REPO_ROOT, "configs/pools.yaml")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "data/sft")

ROUTER_PASS_K = 3


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def router_probe(
    question: str, gold: str, source: str,
    args: argparse.Namespace, pools: PoolConfig,
) -> bool:
    """Stage 1: test if the current router can already solve this task."""
    for _ in range(ROUTER_PASS_K):
        result = run_agent(
            question, args.router_model,
            args.router_api_base, args.router_api_key,
            args.sub_model_api_base, args.sub_model_api_key,
            pools, temperature=0.7,
        )
        if result["complete"] and verify(result["answer"], gold, source):
            return True
    return False


def teacher_run(
    question: str, gold: str, source: str,
    args: argparse.Namespace, pools: PoolConfig,
) -> tuple[bool, dict]:
    """Stage 2: collect a teacher demonstration trajectory."""
    result = run_agent(
        question, args.teacher_model,
        args.teacher_api_base, args.teacher_api_key,
        args.sub_model_api_base, args.sub_model_api_key,
        pools, temperature=0.3,
    )
    ok = result["complete"] and verify(result["answer"], gold, source)
    return ok, result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrapped curriculum filtering pipeline")
    parser.add_argument("--recipe", default=RECIPE_PATH)
    parser.add_argument("--pools", default=POOLS_PATH)
    parser.add_argument("--router-model", required=True)
    parser.add_argument("--router-api-base", required=True)
    parser.add_argument("--router-api-key", default="none")
    parser.add_argument("--teacher-model", default="claude-sonnet-4-6")
    parser.add_argument("--teacher-api-base", required=True)
    parser.add_argument("--teacher-api-key", required=True)
    parser.add_argument("--sub-model-api-base", required=True)
    parser.add_argument("--sub-model-api-key", default="none")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tasks", type=int, default=None)
    args = parser.parse_args()

    random.seed(args.seed)
    pools = load_pools(args.pools)
    recipe = load_recipe(args.recipe)
    print(f"Model pool: {pools['models']}")
    print(f"Skill pool: {pools['skills']}\n")

    print("Loading question pool...")
    all_tasks = load_question_pool(recipe)
    if args.max_tasks:
        random.shuffle(all_tasks)
        all_tasks = all_tasks[:args.max_tasks]
    print(f"Total: {len(all_tasks)} tasks\n")

    os.makedirs(args.out_dir, exist_ok=True)
    sft_path = os.path.join(args.out_dir, "sft.jsonl")
    rl_path = os.path.join(args.out_dir, "rl.jsonl")
    report_path = os.path.join(args.out_dir, "curriculum_report.json")

    sft_data, rl_data = [], []
    stats = {
        "total": len(all_tasks), "router_ok": 0, "sft": 0, "rl": 0, "overlong": 0,
        "per_source": defaultdict(lambda: {"total": 0, "router_ok": 0, "sft": 0, "rl": 0}),
    }

    for task in tqdm(all_tasks, desc="Pipeline"):
        q, gold, src = task["question"], task["gold_answer"], task["source"]
        stats["per_source"][src]["total"] += 1

        # Stage 1: Router probe
        if router_probe(q, gold, src, args, pools):
            stats["router_ok"] += 1
            stats["per_source"][src]["router_ok"] += 1
            continue

        # Stage 2: Teacher trajectory
        teacher_ok, teacher_result = teacher_run(q, gold, src, args, pools)
        if not teacher_ok:
            rl_data.append({"question": q, "gold_answer": gold, "source": src, "domain": task["domain"]})
            stats["rl"] += 1
            stats["per_source"][src]["rl"] += 1
            continue

        # Stage 3: Overlong filter + pack
        keep, packed = filter_and_pack(task, teacher_result)
        if not keep:
            stats["overlong"] += 1
            continue

        sft_data.append(packed)
        stats["sft"] += 1
        stats["per_source"][src]["sft"] += 1

    # Write outputs
    with open(sft_path, "w") as f:
        for s in sft_data:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with open(rl_path, "w") as f:
        for s in rl_data:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    stats["per_source"] = {k: dict(v) for k, v in stats["per_source"].items()}
    with open(report_path, "w") as f:
        json.dump(stats, f, indent=2)

    # Report
    print(f"\nTotal:             {stats['total']}")
    print(f"Router already OK: {stats['router_ok']} -> dropped")
    print(f"SFT (teacher OK):  {stats['sft']} -> {sft_path}")
    print(f"RL (teacher fail): {stats['rl']} -> {rl_path}")
    print(f"Overlong dropped:  {stats['overlong']}")
    print(f"\nPer-source:")
    for src, sc in sorted(stats["per_source"].items(), key=lambda x: -x[1]["total"]):
        rok_pct = sc["router_ok"] / max(sc["total"], 1) * 100
        print(f"  {src:20s} total={sc['total']:>5d}  router_ok={sc['router_ok']:>4d} ({rok_pct:4.1f}%)  sft={sc['sft']:>4d}  rl={sc['rl']:>4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
