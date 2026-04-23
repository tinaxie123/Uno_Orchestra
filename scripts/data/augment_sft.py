"""
Rejection-sampled + difficulty-aware augmentation of the SFT trajectory pool.

For each task we draw K additional teacher trajectories at varied planner
temperatures and keep only those whose final answer passes the per-source
verifier. Hard tasks (from the RL pool) receive more samples than easy ones.

Pipeline (standard rejection sampling, e.g. STaR/RFT/ReST + DART-style
difficulty-aware budget):

    for question q in (SFT pool ∪ hard-RL subset):
        K = 2 if q ∈ SFT pool else K_hard
        for T in sampled_temperatures(K):
            trajectory ← arun_agent(q, planner_temp=T)
            if verifier(trajectory.answer, q.gold):
                keep
    merge with existing SFT → dedup on embedding similarity → SFT-v2

All endpoints are routed through xiaojingai (REMOTE_API_BASE). No local GPU
required. Progress is streamed to a JSONL file and resumable via --resume.

Usage
-----
    # Sensible defaults: K=2 temp variants on SFT, K=2 on 1k hard tasks.
    python scripts/data/augment_sft.py \\
        --sft-parquet data/sft/merged/sft.parquet \\
        --rl-trajectories data/sft/merged/trajectories/trajectories.jsonl \\
        --out data/sft/merged/sft_aug.jsonl \\
        --hard-tasks 1000 \\
        --concurrency 24
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
from configs import load_pools
from scripts.data.agent import arun_agent
from scripts.data.verifiers import verify


REMOTE_API_BASE = os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")
REMOTE_API_KEY = os.environ.get(
    "REMOTE_API_KEY", "sk-7Yv0jGTYpFErrvpnVIlz3cGOtxIuwucfMT5fSxYalwVWprL0",
)

# Teacher = orchestrator that drives planner+router. xiaojingai exposes Qwen
# via alias 'qwen3.5-plus' (cheap, fast). Fall back to claude-sonnet if alias
# changes.
TEACHER_MODEL = os.environ.get("TEACHER_MODEL", "qwen3.5-plus")

# Temperature schedule — each K corresponds to one temp draw from this list.
TEMP_SCHEDULE = [0.5, 1.0]          # used for easy (SFT-pool) tasks
TEMP_SCHEDULE_HARD = [0.3, 0.7, 1.0]  # used for hard (RL-pool) tasks


def pick_hard_tasks(rl_jsonl: Path, k: int, rng: random.Random) -> list[dict]:
    """Pick k 'hardest' tasks from the RL pool — those the teacher failed.

    We pick sources in proportion to their RL-pool shares so we preserve the
    capability axis mix.
    """
    rows = []
    with rl_jsonl.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("teacher_ok"):
                continue  # RL pool = teacher failed
            rows.append({
                "question": d["question"],
                "gold_answer": d["gold_answer"],
                "source": d["source"],
                "domain": d["domain"],
            })
    rng.shuffle(rows)
    return rows[:k]


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


async def _try_one(
    item: dict, temp: float, pools, semaphore: asyncio.Semaphore, attempt_k: int,
) -> dict:
    """Run a single teacher rollout. Return a JSON-ready record."""
    async with semaphore:
        t0 = time.time()
        try:
            result = await arun_agent(
                question=item["question"],
                planner_model=TEACHER_MODEL,
                planner_api_base=REMOTE_API_BASE,
                planner_api_key=REMOTE_API_KEY,
                router_model=TEACHER_MODEL,
                router_api_base=REMOTE_API_BASE,
                router_api_key=REMOTE_API_KEY,
                sub_model_api_base=REMOTE_API_BASE,
                sub_model_api_key=REMOTE_API_KEY,
                pools=pools,
                planner_temperature=temp,
                router_temperature=min(0.7, max(0.3, temp - 0.2)),
                domain=item.get("domain"),
                source=item.get("source"),
            )
            answer = (result.get("answer") or "").strip()
            complete = bool(result.get("complete"))
            ok = complete and verify(answer, item["gold_answer"], item["source"])
            return {
                "question": item["question"],
                "gold_answer": item["gold_answer"],
                "source": item["source"],
                "domain": item.get("domain"),
                "attempt_k": attempt_k,
                "temperature": temp,
                "teacher_ok": ok,
                "elapsed_s": round(time.time() - t0, 1),
                "trajectory": _sanitize(result),
            }
        except Exception as e:
            return {
                "question": item["question"],
                "gold_answer": item["gold_answer"],
                "source": item["source"],
                "domain": item.get("domain"),
                "attempt_k": attempt_k,
                "temperature": temp,
                "teacher_ok": False,
                "elapsed_s": round(time.time() - t0, 1),
                "error": str(e)[:400],
            }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sft-parquet", default="data/sft/merged/sft.parquet",
                    help="Existing SFT parquet whose questions we augment.")
    ap.add_argument("--rl-trajectories",
                    default="data/sft/merged/trajectories/trajectories.jsonl",
                    help="Merged teacher trajectories JSONL; RL pool comes from teacher_ok=False.")
    ap.add_argument("--pools", default="configs/pools.yaml")
    ap.add_argument("--out", default="data/sft/merged/sft_aug.jsonl",
                    help="Output JSONL — appended with every completed rollout.")
    ap.add_argument("--hard-tasks", type=int, default=1000,
                    help="Number of hardest (RL-pool) tasks to include.")
    ap.add_argument("--concurrency", type=int, default=24,
                    help="Max concurrent teacher rollouts.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip (question, temperature) pairs already in --out.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-tasks", type=int, default=None,
                    help="Optional cap on total tasks (debugging).")
    args = ap.parse_args()

    pools = load_pools(args.pools)
    rng = random.Random(args.seed)

    sft_df = pd.read_parquet(args.sft_parquet)
    sft_tasks = [
        {
            "question": str(r["question"]),
            "gold_answer": str(r["gold_answer"]),
            "source": str(r["source"]),
            "domain": str(r["domain"]),
            "pool": "sft",
        }
        for _, r in sft_df.iterrows()
    ]

    hard_tasks = []
    if args.hard_tasks and Path(args.rl_trajectories).exists():
        raw_hard = pick_hard_tasks(Path(args.rl_trajectories), args.hard_tasks, rng)
        for t in raw_hard:
            t["pool"] = "rl_hard"
            hard_tasks.append(t)
    tasks = sft_tasks + hard_tasks
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]

    # Expand into per-sample work items
    work: list[tuple[dict, float, int]] = []
    for t in tasks:
        schedule = TEMP_SCHEDULE_HARD if t["pool"] == "rl_hard" else TEMP_SCHEDULE
        for k, temp in enumerate(schedule):
            work.append((t, temp, k))

    # Resume: skip items already in --out
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple[str, float, int]] = set()
    if args.resume and out_path.exists():
        for line in out_path.open():
            try:
                d = json.loads(line)
                done.add((d["question"], d.get("temperature", 0), d.get("attempt_k", 0)))
            except Exception:
                pass
    pending = [(t, temp, k) for t, temp, k in work
               if (t["question"], temp, k) not in done]
    print(f"[setup] sft tasks: {len(sft_tasks)}  hard tasks: {len(hard_tasks)}  "
          f"total work items: {len(work)}  pending: {len(pending)}  "
          f"resumed from: {len(done)}")
    print(f"[setup] teacher model: {TEACHER_MODEL}  api_base: {REMOTE_API_BASE}")

    sem = asyncio.Semaphore(args.concurrency)
    n_pass = n_fail = n_err = 0
    pool_kept: Counter[str] = Counter()
    t_start = time.time()

    async def _consume():
        nonlocal n_pass, n_fail, n_err
        coros = [_try_one(t, temp, pools, sem, k) for (t, temp, k) in pending]
        with out_path.open("a") as fout:
            for fut in asyncio.as_completed(coros):
                rec = await fut
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush()
                if rec.get("error"):
                    n_err += 1
                elif rec.get("teacher_ok"):
                    n_pass += 1
                    # find the task's pool
                    for t in tasks:
                        if t["question"] == rec["question"]:
                            pool_kept[t["pool"]] += 1; break
                else:
                    n_fail += 1
                total = n_pass + n_fail + n_err
                if total % 25 == 0:
                    elapsed = (time.time() - t_start) / 60
                    rate = total / max(1, (time.time() - t_start))
                    print(f"  progress {total}/{len(pending)}  pass={n_pass} fail={n_fail} err={n_err}  "
                          f"{rate:.1f}/s  elapsed {elapsed:.1f}min")

    await _consume()

    print(f"\n==== augmentation summary ====")
    print(f"  total rollouts: {n_pass + n_fail + n_err}")
    print(f"  teacher_ok:     {n_pass}")
    print(f"    by pool:      {dict(pool_kept)}")
    print(f"  teacher_fail:   {n_fail}")
    print(f"  errors:         {n_err}")
    print(f"  wall clock:     {(time.time() - t_start)/60:.1f} min")
    print(f"  output:         {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
