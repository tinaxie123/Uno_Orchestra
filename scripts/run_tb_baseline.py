"""
Run a Terminal-Bench baseline across all (or a subset of) tasks.

Usage
-----
    python scripts/run_tb_baseline.py --baseline <name> \
        [--tasks N] [--out results.jsonl] [--concurrency 1]

Baselines (name → (planner config, worker pool config)):
    direct-qwen7b  : Qwen/Qwen2.5-7B-Instruct (planner + worker), local vLLM
    direct-claude  : claude-opus-4-6 (planner + worker), via xiaojingai
    random         : Qwen planner, random worker per delegation (local+remote mixed pool)
    router+claude  : Qwen planner, Claude-only worker pool
    router-r1      : external router (todo)

Results are written as JSONL: one line per task with
    {task_id, baseline, reward, cost, tokens, attempts_used, submit_called,
     time_seconds, error}
plus an aggregate summary printed at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval_pipeline.benchmarks.terminalbench import TerminalBench
from eval_pipeline.routers.direct import DirectRouter
from eval_pipeline.routers.oracle import router_plus_claude
from eval_pipeline.routers.oracle import OracleRouter
from eval_pipeline.routers.random_router import RandomRouter
from eval_pipeline.config import MODEL_POOL

# ---- endpoints (override with env vars) -----------------------------------
LOCAL_VLLM = os.environ.get("LOCAL_VLLM_BASE", "http://localhost:8234/v1")
REMOTE_API = os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")
REMOTE_KEY = os.environ.get("REMOTE_API_KEY", "sk-Koqmqsvz6RO0ptLNuXRsZRXnzSPdZzkrqr6FL4M5HcbvMS4Q")


def build(baseline: str):
    """Return (router, worker_pool, subagent_api_base, subagent_api_key, flat_mode)."""
    if baseline == "direct-qwen7b":
        return (
            DirectRouter(
                model_id="Qwen/Qwen2.5-7B-Instruct",
                api_base=LOCAL_VLLM, api_key="EMPTY",
            ),
            None, None, None, True,  # flat mode: no delegation layer
        )
    if baseline == "direct-claude":
        return (
            OracleRouter(
                "claude-opus-4-6", "Direct(claude-opus)",
                api_base=REMOTE_API, api_key=REMOTE_KEY,
            ),
            None, None, None, True,  # flat mode
        )
    if baseline == "router+claude":
        # Hierarchical: Qwen-7B planner delegates to Claude workers only.
        return (
            DirectRouter(
                model_id="Qwen/Qwen2.5-7B-Instruct",
                api_base=LOCAL_VLLM, api_key="EMPTY",
            ),
            ["claude-opus-4-6"],
            REMOTE_API, REMOTE_KEY, False,
        )
    if baseline == "random":
        # Hierarchical: Qwen-7B planner, random worker from the full pool.
        return (
            DirectRouter(
                model_id="Qwen/Qwen2.5-7B-Instruct",
                api_base=LOCAL_VLLM, api_key="EMPTY",
            ),
            list(MODEL_POOL),
            REMOTE_API, REMOTE_KEY, False,
        )
    raise ValueError(f"unknown baseline: {baseline}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True,
                    choices=["direct-qwen7b", "direct-claude", "router+claude", "random"])
    ap.add_argument("--tasks", type=int, default=None,
                    help="Limit to first N tasks (default: all 89).")
    ap.add_argument("--task-ids", default=None,
                    help="Comma-separated subset of task_ids.")
    ap.add_argument("--out", default=None, help="Output JSONL path.")
    ap.add_argument("--logs", default=None, help="Per-task logs dir.")
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--subagent-max-steps", type=int, default=15)
    ap.add_argument("--resume", action="store_true",
                    help="Skip tasks already in --out.")
    args = ap.parse_args()

    out = Path(args.out or f"results/tb_{args.baseline}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    logs = Path(args.logs or f"logs/tb_{args.baseline}")
    logs.mkdir(parents=True, exist_ok=True)

    done_ids: set[str] = set()
    if args.resume and out.exists():
        for line in out.open():
            try:
                done_ids.add(json.loads(line)["task_id"])
            except Exception:
                pass
    print(f"[setup] resume: {len(done_ids)} tasks already done")

    tb = TerminalBench(
        max_attempts=args.max_attempts,
        subagent_max_steps=args.subagent_max_steps,
        subagent_cmd_timeout=120,
    )
    all_tasks = tb.load()
    if args.task_ids:
        want = set(t.strip() for t in args.task_ids.split(",") if t.strip())
        all_tasks = [t for t in all_tasks if t.task_id in want]
    elif args.tasks:
        all_tasks = all_tasks[: args.tasks]
    tasks = [t for t in all_tasks if t.task_id not in done_ids]
    print(f"[setup] baseline={args.baseline}  to-run={len(tasks)}  skipped={len(all_tasks)-len(tasks)}")

    router, pool, sub_base, sub_key, flat = build(args.baseline)
    print(f"[setup] router={router.name}  pool={pool}  flat_mode={flat}")

    t_start = time.time()
    n_pass = n_fail = 0
    cumulative_reward = 0.0

    with out.open("a") as fout:
        for i, task in enumerate(tasks):
            t0 = time.time()
            print(f"\n[{i+1:>3}/{len(tasks)}] {task.task_id}", flush=True)
            record = {
                "task_id": task.task_id,
                "baseline": args.baseline,
                "reward": 0.0,
                "time_seconds": 0.0,
                "attempts_used": 0,
                "submit_called": False,
                "error": None,
            }
            try:
                res = tb.run_interactive(
                    task, router,
                    worker_pool=pool,
                    subagent_api_base=sub_base,
                    subagent_api_key=sub_key or "EMPTY",
                    logs_dir=str(logs),
                    flat_mode=flat,
                )
                record["reward"] = float(res.reward or 0.0)
                record["error"] = res.error
                # Attempt count + submit from trajectory.json
                traj_path = logs / task.task_id / "trajectory.json"
                if traj_path.exists():
                    tj = json.loads(traj_path.read_text())
                    record["attempts_used"] = tj.get("attempts_used", 0)
                    record["submit_called"] = tj.get("submit_called", False)
            except Exception as e:
                tb_str = traceback.format_exc()[-500:]
                record["error"] = f"runner crash: {e}"
                record["traceback"] = tb_str

            record["time_seconds"] = round(time.time() - t0, 1)
            fout.write(json.dumps(record, ensure_ascii=False) + "\n"); fout.flush()

            if record["reward"] >= 1.0:
                n_pass += 1
            else:
                n_fail += 1
            cumulative_reward += record["reward"]
            print(f"    reward={record['reward']:.2f} attempts={record['attempts_used']} "
                  f"submit={record['submit_called']} time={record['time_seconds']:.0f}s "
                  f"err={record['error'] or ''}")

            n_done_so_far = n_pass + n_fail
            print(f"    running: pass={n_pass} fail={n_fail} rate={n_pass/max(1,n_done_so_far):.1%} "
                  f"elapsed={(time.time()-t_start)/60:.1f}min", flush=True)

    total = len(tasks)
    print(f"\n==== {args.baseline} summary ====")
    print(f"  tasks: {total}, pass: {n_pass}, fail: {n_fail}")
    print(f"  pass@1: {n_pass/max(1,total):.3%}")
    print(f"  avg reward: {cumulative_reward/max(1,total):.3f}")
    print(f"  total time: {(time.time()-t_start)/60:.1f} min")
    print(f"  results: {out}")


if __name__ == "__main__":
    main()
