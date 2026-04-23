"""
Stronger-teacher rescue pass on the RL pool.

For each task in the RL pool (original teacher failed), we retry once with a
stronger teacher model (default: claude-sonnet-4-6) and keep any trajectory
that now passes the per-source verifier. Rescued trajectories graduate from
the RL pool to the SFT pool; the remainder stays in RL.

This follows standard "teacher cascade" practice: run the cheap teacher first,
escalate to a stronger teacher on the long-tail of failures. Lines up with
Self-Taught Reasoner / DART-style difficulty-aware budgeting but explicitly
from the teacher-model axis rather than the temperature axis.

Usage
-----
    # 500-task pilot with Sonnet
    python scripts/data/rescue_rl.py \\
        --n-tasks 500 \\
        --teacher claude-sonnet-4-6 \\
        --out data/sft/merged/rescue_sonnet_500.jsonl \\
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

from configs import load_pools
from scripts.data.agent import arun_agent
from scripts.data.verifiers import verify


REMOTE_API_BASE = os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")
REMOTE_API_KEY = os.environ.get(
    "REMOTE_API_KEY", "sk-7Yv0jGTYpFErrvpnVIlz3cGOtxIuwucfMT5fSxYalwVWprL0",
)


def pick_rl_tasks(rl_jsonl: Path, n: int, seed: int) -> list[dict]:
    rows: list[dict] = []
    with rl_jsonl.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("teacher_ok"):
                continue
            rows.append({
                "question": d["question"],
                "gold_answer": d["gold_answer"],
                "source": d["source"],
                "domain": d["domain"],
            })
    rng = random.Random(seed)
    rng.shuffle(rows)
    return rows[:n]


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


async def _try_one(
    item: dict, teacher: str, pools, semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        t0 = time.time()
        try:
            result = await arun_agent(
                question=item["question"],
                planner_model=teacher,
                planner_api_base=REMOTE_API_BASE,
                planner_api_key=REMOTE_API_KEY,
                router_model=teacher,
                router_api_base=REMOTE_API_BASE,
                router_api_key=REMOTE_API_KEY,
                sub_model_api_base=REMOTE_API_BASE,
                sub_model_api_key=REMOTE_API_KEY,
                pools=pools,
                planner_temperature=0.3,  # stronger teacher + deterministic
                router_temperature=0.2,
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
                "teacher": teacher,
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
                "teacher": teacher,
                "teacher_ok": False,
                "elapsed_s": round(time.time() - t0, 1),
                "error": str(e)[:400],
            }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rl-trajectories",
                    default="data/sft/merged/trajectories/trajectories.jsonl")
    ap.add_argument("--pools", default="configs/pools.yaml")
    ap.add_argument("--teacher", default="claude-sonnet-4-6",
                    help="Stronger teacher model (must resolve via REMOTE_API_BASE).")
    ap.add_argument("--n-tasks", type=int, default=500,
                    help="How many RL-pool tasks to retry.")
    ap.add_argument("--out", default="data/sft/merged/rescue_sonnet_500.jsonl")
    ap.add_argument("--concurrency", type=int, default=24)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    pools = load_pools(args.pools)
    tasks = pick_rl_tasks(Path(args.rl_trajectories), args.n_tasks, args.seed)
    print(f"[setup] teacher: {args.teacher}  api: {REMOTE_API_BASE}")
    print(f"[setup] RL tasks picked: {len(tasks)}")
    src_mix = Counter(t["source"] for t in tasks)
    print(f"        source mix: {dict(src_mix)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume and out_path.exists():
        for line in out_path.open():
            try:
                d = json.loads(line); done.add(d["question"])
            except Exception:
                pass
    pending = [t for t in tasks if t["question"] not in done]
    print(f"        resumed from: {len(done)}, pending: {len(pending)}")

    sem = asyncio.Semaphore(args.concurrency)
    n_ok = n_fail = n_err = 0
    by_source: dict[str, dict[str, int]] = {}
    t_start = time.time()

    coros = [_try_one(t, args.teacher, pools, sem) for t in pending]
    with out_path.open("a") as fout:
        for fut in asyncio.as_completed(coros):
            rec = await fut
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush()
            src = rec["source"]
            by_source.setdefault(src, {"ok": 0, "fail": 0, "err": 0})
            if rec.get("error"):
                n_err += 1; by_source[src]["err"] += 1
            elif rec.get("teacher_ok"):
                n_ok += 1; by_source[src]["ok"] += 1
            else:
                n_fail += 1; by_source[src]["fail"] += 1
            total = n_ok + n_fail + n_err
            if total % 25 == 0:
                elapsed = (time.time() - t_start) / 60
                print(f"  progress {total}/{len(pending)}  ok={n_ok} fail={n_fail} err={n_err}  "
                      f"elapsed={elapsed:.1f}min")

    print(f"\n==== rescue summary ({args.teacher}) ====")
    print(f"  total:      {n_ok + n_fail + n_err}")
    print(f"  teacher_ok: {n_ok}  ({100*n_ok/max(1,n_ok+n_fail+n_err):.1f}%)")
    print(f"  fail:       {n_fail}")
    print(f"  err:        {n_err}")
    print(f"  wall clock: {(time.time() - t_start)/60:.1f} min")
    print("  by source:")
    for s, c in sorted(by_source.items()):
        total = sum(c.values())
        print(f"    {s:<12} ok={c['ok']:>3} fail={c['fail']:>3} err={c['err']:>2}  "
              f"({100*c['ok']/max(1,total):.1f}% pass)")
    print(f"  output:     {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
