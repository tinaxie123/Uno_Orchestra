
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import defaultdict

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.insert(0, REPO_ROOT)

from configs import PoolConfig, load_pools
from scripts.data.agent import arun_agent
from scripts.data.data_loader import load_recipe, load_question_pool
from scripts.data.formatter import filter_and_pack
from scripts.data.verifiers import verify

RECIPE_PATH = os.path.join(REPO_ROOT, "configs/sft/data/sft_recipe.yaml")
POOLS_PATH = os.path.join(REPO_ROOT, "configs/pools.yaml")
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "data/sft")

ROUTER_PASS_K = 3
DEFAULT_CONCURRENCY = 32


async def arouter_probe(
    question: str, gold: str, source: str,
    args: argparse.Namespace, pools: PoolConfig,
) -> tuple[bool, list[dict]]:
    """Stage 1: test if the current router can already solve this task.
    Returns (passed, list_of_attempt_trajectories)."""
    attempts = []
    for k in range(ROUTER_PASS_K):
        result = await arun_agent(
            question=question,
            planner_model=args.planner_model or args.router_model,
            planner_api_base=args.planner_api_base or args.router_api_base,
            planner_api_key=args.planner_api_key or args.router_api_key,
            router_model=args.router_model,
            router_api_base=args.router_api_base,
            router_api_key=args.router_api_key,
            sub_model_api_base=args.sub_model_api_base,
            sub_model_api_key=args.sub_model_api_key,
            pools=pools,
            planner_temperature=0.7,
        )
        ok = result["complete"] and verify(result["answer"], gold, source)
        attempts.append({"attempt": k, "ok": ok, "trajectory": result})
        if ok:
            return True, attempts
    return False, attempts


async def ateacher_run(
    question: str, gold: str, source: str,
    args: argparse.Namespace, pools: PoolConfig,
) -> tuple[bool, dict]:
    """Stage 2: collect a teacher demonstration trajectory."""
    result = await arun_agent(
        question=question,
        planner_model=args.teacher_model,
        planner_api_base=args.teacher_api_base,
        planner_api_key=args.teacher_api_key,
        router_model=args.teacher_model,
        router_api_base=args.teacher_api_base,
        router_api_key=args.teacher_api_key,
        sub_model_api_base=args.sub_model_api_base,
        sub_model_api_key=args.sub_model_api_key,
        pools=pools,
        planner_temperature=0.3,
    )
    ok = result["complete"] and verify(result["answer"], gold, source)
    return ok, result


def _flush_report(report_path: str, progress: dict):
    """Atomically write progress report so the monitor can read it."""
    tmp = report_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"total": progress["total"], "stats": {
            "router_ok": progress["router_ok"],
            "sft": progress["sft"],
            "rl": progress["rl"],
            "overlong": progress["overlong"],
        }}, f)
    os.replace(tmp, report_path)


async def process_one(
    idx: int, task: dict, args: argparse.Namespace,
    pools: PoolConfig, sem: asyncio.Semaphore,
    progress: dict, write_lock: asyncio.Lock,
) -> dict:
    """Process a single task through the full pipeline under semaphore."""
    async with sem:
        q, gold, src = task["question"], task["gold_answer"], task["source"]
        domain = task["domain"]
        t0 = time.time()

        # Stage 1: Router probe
        try:
            router_ok, router_attempts = await arouter_probe(q, gold, src, args, pools)
        except Exception as e:
            router_ok, router_attempts = False, []

        # Save router probe trajectories
        traj_dir = progress.get("traj_dir")
        if traj_dir and router_attempts:
            router_traj = {
                "idx": idx, "question": q, "gold_answer": gold,
                "source": src, "domain": domain,
                "stage": "router_probe", "router_ok": router_ok,
                "attempts": router_attempts,
                "elapsed_s": round(time.time() - t0, 1),
            }
            async with write_lock:
                with open(os.path.join(traj_dir, "router_trajectories.jsonl"), "a") as f:
                    f.write(json.dumps(router_traj, ensure_ascii=False) + "\n")

        if router_ok:
            dt = time.time() - t0
            async with write_lock:
                progress["done"] += 1
                progress["router_ok"] += 1
                _flush_report(progress["report_path"], progress)
            print(f"[{progress['done']}/{progress['total']}] ROUTER_OK  src={src}  ({dt:.1f}s)")
            return {"status": "router_ok", "source": src, "domain": domain}

        # Stage 2: Teacher trajectory
        try:
            teacher_ok, teacher_result = await ateacher_run(q, gold, src, args, pools)
        except Exception as e:
            teacher_ok = False
            teacher_result = {}

        dt = time.time() - t0

        # ── Save full trajectory to disk (regardless of outcome) ──
        traj_dir = progress.get("traj_dir")
        if traj_dir and teacher_result:
            traj_item = {
                "idx": idx, "question": q, "gold_answer": gold,
                "source": src, "domain": domain,
                "teacher_ok": teacher_ok,
                "trajectory": teacher_result,
                "elapsed_s": round(dt, 1),
            }
            async with write_lock:
                with open(os.path.join(traj_dir, "trajectories.jsonl"), "a") as f:
                    f.write(json.dumps(traj_item, ensure_ascii=False) + "\n")

        if not teacher_ok:
            rl_item = {"question": q, "gold_answer": gold, "source": src, "domain": domain}
            async with write_lock:
                progress["done"] += 1
                progress["rl"] += 1
                with open(progress["rl_path"], "a") as f:
                    f.write(json.dumps(rl_item, ensure_ascii=False) + "\n")
                _flush_report(progress["report_path"], progress)
            print(f"[{progress['done']}/{progress['total']}] RL         src={src}  ({dt:.1f}s)")
            return {"status": "rl", "source": src, "domain": domain}

        # Stage 3: Overlong filtering
        keep, packed = filter_and_pack(task, teacher_result)
        if not keep:
            async with write_lock:
                progress["done"] += 1
                progress["overlong"] += 1
                _flush_report(progress["report_path"], progress)
            print(f"[{progress['done']}/{progress['total']}] OVERLONG   src={src}  ({dt:.1f}s)")
            return {"status": "overlong", "source": src, "domain": domain}

        async with write_lock:
            progress["done"] += 1
            progress["sft"] += 1
            with open(progress["sft_path"], "a") as f:
                f.write(json.dumps(packed, ensure_ascii=False) + "\n")
            _flush_report(progress["report_path"], progress)
        print(f"[{progress['done']}/{progress['total']}] SFT        src={src}  ({dt:.1f}s)")
        return {"status": "sft", "source": src, "domain": domain, "sft_item": packed}


async def amain(args: argparse.Namespace) -> int:
    pools = load_pools(args.pools)
    recipe = load_recipe(args.recipe)
    print(f"Model pool: {pools['models']}")
    print(f"Skill pool: {pools['skills']}\n")

    print("Loading question pool...")
    all_tasks = load_question_pool(recipe)
    if args.skip_first:
        print(f"Skipping first {args.skip_first} tasks")
        all_tasks = all_tasks[args.skip_first:]
    if args.max_tasks:
        random.shuffle(all_tasks)
        all_tasks = all_tasks[:args.max_tasks]
    print(f"Total: {len(all_tasks)} tasks, concurrency: {args.concurrency}\n")

    os.makedirs(args.out_dir, exist_ok=True)
    sft_path = os.path.join(args.out_dir, "sft.jsonl")
    rl_path = os.path.join(args.out_dir, "rl.jsonl")
    report_path = os.path.join(args.out_dir, "curriculum_report.json")

    # Trajectory dump directory
    traj_dir = os.path.join(args.out_dir, "trajectories")
    os.makedirs(traj_dir, exist_ok=True)

    # Resume: load already-processed questions from existing sft/rl files
    done_questions = set()
    for p in (sft_path, rl_path):
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with open(p) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        done_questions.add(d.get("question", d.get("conversations", [{}])[0].get("value", "")))
                    except Exception:
                        pass
    # Also count router_ok from existing router trajectories
    router_traj_path = os.path.join(traj_dir, "router_trajectories.jsonl")
    router_ok_count = 0
    if os.path.exists(router_traj_path):
        with open(router_traj_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("router_ok"):
                        done_questions.add(d["question"])
                        router_ok_count += 1
                except Exception:
                    pass

    if done_questions:
        before = len(all_tasks)
        all_tasks = [t for t in all_tasks if t["question"] not in done_questions]
        print(f"Resume: skipping {before - len(all_tasks)} already-processed tasks, {len(all_tasks)} remaining")

    if not all_tasks:
        print("All tasks already processed. Nothing to do.")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()
    progress = {
        "done": 0, "total": len(all_tasks),
        "router_ok": 0, "sft": 0, "rl": 0, "overlong": 0,
        "sft_path": sft_path, "rl_path": rl_path, "report_path": report_path,
        "traj_dir": traj_dir,
    }
    # Write initial report so the monitor picks up total immediately
    _flush_report(report_path, progress)

    t0 = time.time()
    tasks = [process_one(i, task, args, pools, sem, progress, write_lock) for i, task in enumerate(all_tasks)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - t0

    # Final per-source stats
    stats = {
        "total": len(all_tasks), "router_ok": 0, "sft": 0, "rl": 0, "overlong": 0,
        "per_source": defaultdict(lambda: {"total": 0, "router_ok": 0, "sft": 0, "rl": 0, "overlong": 0}),
    }
    for r in results:
        src = r["source"]
        status = r["status"]
        stats[status] += 1
        stats["per_source"][src]["total"] += 1
        stats["per_source"][src][status] += 1

    stats["per_source"] = {k: dict(v) for k, v in stats["per_source"].items()}
    with open(report_path, "w") as f:
        json.dump(stats, f, indent=2)

    print(f"\n{'='*50}")
    print(f"Total:             {stats['total']}  ({elapsed:.0f}s, {elapsed/len(all_tasks):.1f}s/task)")
    print(f"Router already OK: {stats['router_ok']} -> dropped")
    print(f"SFT (teacher OK):  {stats['sft']} -> {sft_path}")
    print(f"RL (teacher fail): {stats['rl']} -> {rl_path}")
    print(f"Overlong dropped:  {stats['overlong']}")
    print(f"\nPer-source:")
    for src, sc in sorted(stats["per_source"].items(), key=lambda x: -x[1]["total"]):
        rok_pct = sc["router_ok"] / max(sc["total"], 1) * 100
        print(f"  {src:20s} total={sc['total']:>5d}  router_ok={sc['router_ok']:>4d} ({rok_pct:4.1f}%)  sft={sc['sft']:>4d}  rl={sc['rl']:>4d}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrapped curriculum filtering pipeline")
    parser.add_argument("--recipe", default=RECIPE_PATH, help="Path to SFT recipe YAML.")
    parser.add_argument("--pools", default=POOLS_PATH, help="Path to model/skill pool YAML.")
    parser.add_argument("--planner-model", default=None, help="Planner model for router probe stage; defaults to --router-model.")
    parser.add_argument("--planner-api-base", default=None, help="Planner API base URL; defaults to --router-api-base.")
    parser.add_argument("--planner-api-key", default=None, help="Planner API key; defaults to --router-api-key.")
    parser.add_argument("--router-model", required=True, help="Router model ID for model+skill selection.")
    parser.add_argument("--router-api-base", required=True, help="Router API base URL.")
    parser.add_argument("--router-api-key", default="none", help="Router API key.")
    parser.add_argument("--teacher-model", default="claude-sonnet-4-6", help="Teacher model used to collect demonstrations.")
    parser.add_argument("--teacher-api-base", required=True, help="Teacher API base URL.")
    parser.add_argument("--teacher-api-key", required=True, help="Teacher API key.")
    parser.add_argument("--sub-model-api-base", required=True, help="Executor sub-model API base URL.")
    parser.add_argument("--sub-model-api-key", default="none", help="Executor sub-model API key.")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Output directory for SFT/RL/report files.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument("--max-tasks", type=int, default=None, help="Optional cap on loaded tasks.")
    parser.add_argument("--skip-first", type=int, default=0, help="Skip first N tasks (for resuming from a known offset).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="Max concurrent tasks.")
    args = parser.parse_args()

    random.seed(args.seed)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
