"""Re-run previously failed trajectories (from rl.jsonl) with infra fixes applied.

Full pipeline: Router probe (pass@3) → Teacher run → SFT/RL split.
Same as generate_trajectories.py but only processes questions from rl.jsonl.

Usage:
    python scripts/data/rerun_failed.py --prev-rl data/sft/round1/rl.jsonl --out-dir data/sft/round1_fixed ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
sys.path.insert(0, REPO_ROOT)

from configs import load_pools
from scripts.data.agent import arun_agent
from scripts.data.formatter import filter_and_pack
from scripts.data.verifiers import verify

POOLS_PATH = os.path.join(REPO_ROOT, "configs/pools.yaml")
DEFAULT_CONCURRENCY = 32
ROUTER_PASS_K = 3


async def arouter_probe(
    question: str, gold: str, source: str, domain: str,
    args: argparse.Namespace, pools,
) -> tuple[bool, list[dict]]:
    """Stage 1: router probe (pass@3)."""
    attempts = []
    for k in range(ROUTER_PASS_K):
        result = await arun_agent(
            question=question,
            planner_model=args.router_model,
            planner_api_base=args.router_api_base,
            planner_api_key=args.router_api_key,
            router_model=args.router_model,
            router_api_base=args.router_api_base,
            router_api_key=args.router_api_key,
            sub_model_api_base=args.sub_model_api_base,
            sub_model_api_key=args.sub_model_api_key,
            pools=pools,
            planner_temperature=0.7,
            domain=domain,
            source=source,
        )
        ok = result["complete"] and verify(result["answer"], gold, source)
        attempts.append({"attempt": k, "ok": ok, "trajectory": result})
        if ok:
            return True, attempts
    return False, attempts


async def process_one(
    idx: int, task: dict, args: argparse.Namespace,
    pools, sem: asyncio.Semaphore,
    progress: dict, write_lock: asyncio.Lock,
) -> dict:
    """Process a single failed task through the full pipeline."""
    async with sem:
        q = task["question"]
        gold = task["gold_answer"]
        src = task["source"]
        domain = task["domain"]
        t0 = time.time()

        # ── Stage 1: Router probe ──
        try:
            router_ok, router_attempts = await arouter_probe(
                q, gold, src, domain, args, pools)
        except Exception:
            router_ok, router_attempts = False, []

        # Save router trajectories
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
            print(f"[{progress['done']}/{progress['total']}] ROUTER_OK  src={src}  ({dt:.1f}s)")
            return {"status": "router_ok", "source": src, "domain": domain}

        # ── Stage 2: Teacher run ──
        try:
            result = await arun_agent(
                question=q,
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
                domain=domain,
                source=src,
            )
            teacher_ok = result["complete"] and verify(result["answer"], gold, src)
        except Exception:
            teacher_ok = False
            result = {"messages": [], "answer": None, "complete": False,
                       "n_delegates": 0, "models_used": [], "skills_used": [],
                       "routing_decisions": [], "subtasks": []}

        dt = time.time() - t0

        # Save teacher trajectory
        if traj_dir:
            traj_item = {
                "idx": idx, "question": q, "gold_answer": gold,
                "source": src, "domain": domain,
                "teacher_ok": teacher_ok, "trajectory": result,
                "elapsed_s": round(dt, 1),
            }
            async with write_lock:
                with open(os.path.join(traj_dir, "trajectories.jsonl"), "a") as f:
                    f.write(json.dumps(traj_item, ensure_ascii=False) + "\n")

        if not teacher_ok:
            rl_item = {"question": q, "gold_answer": gold, "source": src, "domain": domain}
            async with write_lock:
                progress["done"] += 1
                progress["still_rl"] += 1
                with open(progress["rl_path"], "a") as f:
                    f.write(json.dumps(rl_item, ensure_ascii=False) + "\n")
            print(f"[{progress['done']}/{progress['total']}] STILL_RL   src={src}  ({dt:.1f}s)")
            return {"status": "still_rl", "source": src, "domain": domain}

        # ── Stage 3: Overlong filtering + pack SFT ──
        keep, packed = filter_and_pack(task, result)
        if not keep:
            async with write_lock:
                progress["done"] += 1
                progress["overlong"] += 1
            print(f"[{progress['done']}/{progress['total']}] OVERLONG   src={src}  ({dt:.1f}s)")
            return {"status": "overlong", "source": src, "domain": domain}

        async with write_lock:
            progress["done"] += 1
            progress["recovered"] += 1
            with open(progress["sft_path"], "a") as f:
                f.write(json.dumps(packed, ensure_ascii=False) + "\n")
        print(f"[{progress['done']}/{progress['total']}] RECOVERED  src={src}  ({dt:.1f}s)")
        return {"status": "recovered", "source": src, "domain": domain}


async def amain(args: argparse.Namespace) -> int:
    pools = load_pools(args.pools)
    print(f"Model pool: {pools['models']}")

    # Load previously failed tasks
    print(f"\nLoading failed tasks from {args.prev_rl}...")
    all_tasks = []
    with open(args.prev_rl) as f:
        for line in f:
            task = json.loads(line)
            all_tasks.append(task)
    print(f"Loaded {len(all_tasks)} failed tasks")

    # Resume support
    os.makedirs(args.out_dir, exist_ok=True)
    sft_path = os.path.join(args.out_dir, "sft.jsonl")
    rl_path = os.path.join(args.out_dir, "rl.jsonl")
    traj_dir = os.path.join(args.out_dir, "trajectories")
    os.makedirs(traj_dir, exist_ok=True)

    done_questions = set()
    for p in (sft_path, rl_path):
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with open(p) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        q = d.get("question", "")
                        # sft format uses 'conversations' instead of 'question'
                        if not q and "conversations" in d:
                            for c in d["conversations"]:
                                if c.get("from") == "human":
                                    q = c["value"]
                                    break
                        if q:
                            done_questions.add(q)
                    except Exception:
                        pass
    # Also check router_ok
    router_traj_path = os.path.join(traj_dir, "router_trajectories.jsonl")
    if os.path.exists(router_traj_path):
        with open(router_traj_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("router_ok"):
                        done_questions.add(d["question"])
                except Exception:
                    pass

    if done_questions:
        before = len(all_tasks)
        all_tasks = [t for t in all_tasks if t["question"] not in done_questions]
        print(f"Resume: skipping {before - len(all_tasks)} already-processed, {len(all_tasks)} remaining")

    if not all_tasks:
        print("All tasks already processed.")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()
    progress = {
        "done": 0, "total": len(all_tasks),
        "router_ok": 0, "recovered": 0, "still_rl": 0, "overlong": 0,
        "sft_path": sft_path, "rl_path": rl_path, "traj_dir": traj_dir,
    }

    t0 = time.time()
    tasks = [process_one(i, task, args, pools, sem, progress, write_lock)
             for i, task in enumerate(all_tasks)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - t0

    # Stats
    stats = defaultdict(lambda: defaultdict(int))
    for r in results:
        stats[r["source"]][r["status"]] += 1
        stats["_total"][r["status"]] += 1

    print(f"\n{'='*60}")
    print(f"Re-run complete: {len(all_tasks)} tasks in {elapsed:.0f}s ({elapsed/len(all_tasks):.1f}s/task)")
    print(f"  Router now OK:       {progress['router_ok']}")
    print(f"  Recovered (new SFT): {progress['recovered']}")
    print(f"  Still RL:            {progress['still_rl']}")
    print(f"  Overlong:            {progress['overlong']}")
    total_recovered = progress['router_ok'] + progress['recovered']
    recovery_rate = total_recovered / max(len(all_tasks), 1) * 100
    print(f"  Total recovery rate: {recovery_rate:.1f}% ({total_recovered}/{len(all_tasks)})")
    print(f"\nPer-source:")
    for src in sorted(stats.keys()):
        if src == "_total":
            continue
        s = stats[src]
        total = sum(s.values())
        rok = s.get("router_ok", 0)
        rec = s.get("recovered", 0)
        print(f"  {src:20s} total={total:>5d}  router_ok={rok:>4d}  recovered={rec:>4d} ({(rok+rec)*100/max(total,1):.1f}%)  still_rl={s.get('still_rl',0):>4d}")

    # Save report
    report = {
        "total": len(all_tasks),
        "router_ok": progress["router_ok"],
        "recovered": progress["recovered"],
        "still_rl": progress["still_rl"],
        "overlong": progress["overlong"],
        "recovery_rate_pct": round(recovery_rate, 1),
        "per_source": {k: dict(v) for k, v in stats.items() if k != "_total"},
    }
    with open(os.path.join(args.out_dir, "rerun_report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run failed trajectories with infra fixes")
    parser.add_argument("--prev-rl", required=True, help="Path to previous round's rl.jsonl")
    parser.add_argument("--pools", default=POOLS_PATH)
    parser.add_argument("--router-model", required=True, help="Router model for probe stage")
    parser.add_argument("--router-api-base", required=True)
    parser.add_argument("--router-api-key", default="none")
    parser.add_argument("--teacher-model", default="qwen3.5-plus")
    parser.add_argument("--teacher-api-base", required=True)
    parser.add_argument("--teacher-api-key", required=True)
    parser.add_argument("--sub-model-api-base", required=True)
    parser.add_argument("--sub-model-api-key", default="none")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
