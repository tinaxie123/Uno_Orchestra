"""
Rescue RL-pool tasks: retry teacher-failed tasks with stronger models.

Bootstrapped curriculum Stage 2 upgrade: for each task in the RL pool
(where the original qwen3.5-plus teacher failed), we retry with a
stronger teacher cascade and pass@3. Tasks that succeed are promoted
from RL→SFT.

Cascade (cheapest-first, stop on first success):
  1. gemini-2.5-pro   (cheap but strong reasoning)
  2. claude-sonnet-4-6 (frontier)
  3. gpt-5.4           (frontier)

Each model gets up to 3 attempts (pass@3). A task is rescued if *any*
attempt by *any* model passes the verifier.

Usage
-----
    python scripts/data/rescue_rl_pool.py \
        --rl-trajectories data/sft/merged/trajectories/trajectories.jsonl \
        --out data/sft/merged/rescued.jsonl \
        --concurrency 16
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

from configs import load_pools
from scripts.data.agent import arun_agent
from scripts.data.verifiers import verify

REMOTE_API_BASE = os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")
REMOTE_API_KEY = os.environ.get(
    "REMOTE_API_KEY", "sk-7Yv0jGTYpFErrvpnVIlz3cGOtxIuwucfMT5fSxYalwVWprL0",
)

# Teacher cascade: try cheaper first, escalate on failure.
# Set via --cascade flag; default is gemini-2.5-pro only (fast).
DEFAULT_CASCADE = [
    ("gemini-2.5-pro", 0.7),
]
FULL_CASCADE = [
    ("gemini-2.5-pro", 0.7),
    ("claude-sonnet-4-6", 0.7),
    ("gpt-5.4", 0.5),
]

PASS_K = 3  # attempts per model


def load_rl_tasks(path: Path) -> list[dict]:
    """Load teacher_ok=False tasks from trajectories JSONL."""
    tasks = []
    with path.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("teacher_ok"):
                continue
            tasks.append({
                "question": d["question"],
                "gold_answer": d["gold_answer"],
                "source": d["source"],
                "domain": d.get("domain", ""),
            })
    return tasks


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


async def _try_one(
    item: dict, teacher_model: str, temp: float,
    pools, semaphore: asyncio.Semaphore, attempt: int,
) -> dict | None:
    """Single teacher attempt. Returns record if pass, None if fail."""
    async with semaphore:
        t0 = time.time()
        try:
            result = await arun_agent(
                question=item["question"],
                planner_model=teacher_model,
                planner_api_base=REMOTE_API_BASE,
                planner_api_key=REMOTE_API_KEY,
                router_model=teacher_model,
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
            elapsed = round(time.time() - t0, 1)
            if ok:
                return {
                    "question": item["question"],
                    "gold_answer": item["gold_answer"],
                    "source": item["source"],
                    "domain": item.get("domain"),
                    "teacher_model": teacher_model,
                    "attempt": attempt,
                    "temperature": temp,
                    "teacher_ok": True,
                    "elapsed_s": elapsed,
                    "trajectory": _sanitize(result),
                }
            return None
        except Exception:
            return None


async def rescue_one(
    item: dict, pools, semaphore: asyncio.Semaphore,
    cascade: list[tuple[str, float]] | None = None,
) -> dict | None:
    """Try cascade of models × pass@k. Return first success or None."""
    for teacher_model, temp in (cascade or DEFAULT_CASCADE):
        for attempt in range(PASS_K):
            rec = await _try_one(item, teacher_model, temp, pools, semaphore, attempt)
            if rec is not None:
                return rec
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rl-trajectories",
                    default="data/sft/merged/trajectories/trajectories.jsonl")
    ap.add_argument("--pools", default="configs/pools.yaml")
    ap.add_argument("--out", default="data/sft/merged/rescued.jsonl")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-tasks", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    # Priority sources: focus on domains with large RL pools
    ap.add_argument("--sources", type=str, default=None,
                    help="Comma-separated sources to rescue (default: all)")
    ap.add_argument("--full-cascade", action="store_true",
                    help="Use full cascade (gemini→claude→gpt). Default: gemini-2.5-pro only.")
    args = ap.parse_args()

    pools = load_pools(args.pools)
    rng = random.Random(args.seed)

    rl_tasks = load_rl_tasks(Path(args.rl_trajectories))
    if args.sources:
        allowed = set(s.strip() for s in args.sources.split(","))
        rl_tasks = [t for t in rl_tasks if t["source"] in allowed]
    rng.shuffle(rl_tasks)
    if args.max_tasks:
        rl_tasks = rl_tasks[:args.max_tasks]

    # Resume
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done_questions: set[str] = set()
    if args.resume and out_path.exists():
        with out_path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                    done_questions.add(d["question"])
                except Exception:
                    pass
    pending = [t for t in rl_tasks if t["question"] not in done_questions]

    src_dist = Counter(t["source"] for t in pending)
    print(f"[setup] RL tasks: {len(rl_tasks)}  pending: {len(pending)}  "
          f"resumed: {len(done_questions)}")
    print(f"[setup] by source: {dict(src_dist)}")
    cascade = FULL_CASCADE if args.full_cascade else DEFAULT_CASCADE
    print(f"[setup] cascade: {[(m,t) for m,t in cascade]}  pass@{PASS_K}")
    print(f"[setup] api_base: {REMOTE_API_BASE}")

    sem = asyncio.Semaphore(args.concurrency)
    n_rescued = n_failed = 0
    rescued_src: Counter[str] = Counter()
    rescued_model: Counter[str] = Counter()
    t_start = time.time()

    async def _consume():
        nonlocal n_rescued, n_failed
        cascade = FULL_CASCADE if args.full_cascade else DEFAULT_CASCADE
        coros = [rescue_one(t, pools, sem, cascade) for t in pending]
        with out_path.open("a") as fout:
            for fut in asyncio.as_completed(coros):
                rec = await fut
                if rec is not None:
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fout.flush()
                    n_rescued += 1
                    rescued_src[rec["source"]] += 1
                    rescued_model[rec["teacher_model"]] += 1
                else:
                    # Log failed task too (for tracking)
                    n_failed += 1
                total = n_rescued + n_failed
                if total % 20 == 0:
                    elapsed = (time.time() - t_start) / 60
                    rate = total / max(1, time.time() - t_start)
                    print(f"  [{total}/{len(pending)}]  rescued={n_rescued} "
                          f"failed={n_failed}  "
                          f"rate={rate:.2f}/s  {elapsed:.1f}min  "
                          f"by_model={dict(rescued_model)}")

    await _consume()

    print(f"\n==== rescue summary ====")
    print(f"  total attempted:  {n_rescued + n_failed}")
    print(f"  rescued (RL→SFT): {n_rescued}")
    print(f"    by source:      {dict(rescued_src)}")
    print(f"    by model:       {dict(rescued_model)}")
    print(f"  still RL:         {n_failed}")
    print(f"  rescue rate:      {n_rescued / max(n_rescued + n_failed, 1) * 100:.1f}%")
    print(f"  wall clock:       {(time.time() - t_start) / 60:.1f} min")
    print(f"  output:           {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
