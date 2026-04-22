"""
Run a Terminal-Bench baseline across all (or a subset of) tasks.

Usage
-----
    python scripts/run_tb_baseline.py --baseline <name> \
        [--tasks N] [--out results.jsonl] [--logs logs/]

Baselines reuse the two-step Planner → Router → Worker schema (matches SFT
training):

    direct-qwen7b   : Qwen2.5-7B-Instruct as both Planner and Router,
                      full (model, skill) pool, workers via xiaojingai/vLLM.
    direct-claude   : Claude-Opus-4.6 as Planner and Router (so Claude
                      chooses (model, skill) for every subtask), same pool.
    random          : Qwen2.5-7B Planner + Router, worker pick is overridden
                      to a uniform random (model, skill) per subtask.
    router+claude   : Qwen2.5-7B Planner + Router, pool restricted to
                      {claude-opus-4-6} × allowed skills.

For ``direct-*-flat`` append ``--flat`` to run a single-model baseline where
the model itself drives the shell via ``execute_command``/``submit`` tools —
useful as an appendix reference point but NOT the main baseline.
"""
from __future__ import annotations

import argparse
import copy
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
from eval_pipeline.routers.oracle import OracleRouter
from configs import load_pools

# ---- endpoints (override with env vars) -----------------------------------
LOCAL_VLLM = os.environ.get("LOCAL_VLLM_BASE", "http://localhost:8234/v1")
REMOTE_API = os.environ.get("REMOTE_API_BASE", "https://open.xiaojingai.com/v1/")
REMOTE_KEY = os.environ.get("REMOTE_API_KEY", "sk-Koqmqsvz6RO0ptLNuXRsZRXnzSPdZzkrqr6FL4M5HcbvMS4Q")

POOLS_PATH = ROOT / "configs" / "pools.yaml"


def _restricted_pool(base_pool: dict, keep_models: list[str]) -> dict:
    p = copy.deepcopy(base_pool)
    p["models"] = [m for m in p["models"] if m in keep_models]
    if "model_skills" in p:
        p["model_skills"] = {
            m: skills for m, skills in p["model_skills"].items() if m in keep_models
        }
    return p


def build(baseline: str):
    """Return a dict with all config a baseline needs.

    Keys
    ----
    mode: "hierarchical" or "flat"
    planner_model / planner_api_base / planner_api_key
    router_model / router_api_base / router_api_key  (hierarchical only)
    sub_model_api_base / sub_model_api_key           (hierarchical only)
    pools                                            (hierarchical only)
    random_worker                                    (hierarchical only)
    flat_router                                      (flat only — a BaseRouter)
    """
    pools = load_pools(POOLS_PATH)

    if baseline == "direct-qwen7b":
        return {
            "mode": "hierarchical",
            "planner_model": "Qwen/Qwen2.5-7B-Instruct",
            "planner_api_base": LOCAL_VLLM,
            "planner_api_key": "EMPTY",
            "router_model": "Qwen/Qwen2.5-7B-Instruct",
            "router_api_base": LOCAL_VLLM,
            "router_api_key": "EMPTY",
            "sub_model_api_base": REMOTE_API,
            "sub_model_api_key": REMOTE_KEY,
            "pools": pools,
            "random_worker": False,
        }
    if baseline == "direct-claude":
        return {
            "mode": "hierarchical",
            "planner_model": "claude-opus-4-6",
            "planner_api_base": REMOTE_API,
            "planner_api_key": REMOTE_KEY,
            "router_model": "claude-opus-4-6",
            "router_api_base": REMOTE_API,
            "router_api_key": REMOTE_KEY,
            "sub_model_api_base": REMOTE_API,
            "sub_model_api_key": REMOTE_KEY,
            "pools": pools,
            "random_worker": False,
        }
    if baseline == "router+claude":
        return {
            "mode": "hierarchical",
            "planner_model": "Qwen/Qwen2.5-7B-Instruct",
            "planner_api_base": LOCAL_VLLM,
            "planner_api_key": "EMPTY",
            "router_model": "Qwen/Qwen2.5-7B-Instruct",
            "router_api_base": LOCAL_VLLM,
            "router_api_key": "EMPTY",
            "sub_model_api_base": REMOTE_API,
            "sub_model_api_key": REMOTE_KEY,
            "pools": _restricted_pool(pools, ["claude-opus-4-6"]),
            "random_worker": False,
        }
    if baseline == "random":
        return {
            "mode": "hierarchical",
            "planner_model": "Qwen/Qwen2.5-7B-Instruct",
            "planner_api_base": LOCAL_VLLM,
            "planner_api_key": "EMPTY",
            "router_model": "Qwen/Qwen2.5-7B-Instruct",
            "router_api_base": LOCAL_VLLM,
            "router_api_key": "EMPTY",
            "sub_model_api_base": REMOTE_API,
            "sub_model_api_key": REMOTE_KEY,
            "pools": pools,
            "random_worker": True,
        }
    # --- flat (appendix) variants ---
    if baseline == "direct-qwen7b-flat":
        return {
            "mode": "flat",
            "flat_router": DirectRouter(
                model_id="Qwen/Qwen2.5-7B-Instruct",
                api_base=LOCAL_VLLM, api_key="EMPTY",
            ),
        }
    if baseline == "direct-claude-flat":
        return {
            "mode": "flat",
            "flat_router": OracleRouter(
                "claude-opus-4-6", "Direct(claude-opus)",
                api_base=REMOTE_API, api_key=REMOTE_KEY,
            ),
        }
    raise ValueError(f"unknown baseline: {baseline}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True,
                    choices=[
                        "direct-qwen7b", "direct-claude", "random", "router+claude",
                        "direct-qwen7b-flat", "direct-claude-flat",
                    ])
    ap.add_argument("--tasks", type=int, default=None)
    ap.add_argument("--task-ids", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--logs", default=None)
    ap.add_argument("--subagent-max-steps", type=int, default=15)
    ap.add_argument("--resume", action="store_true")
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

    tb = TerminalBench(subagent_max_steps=args.subagent_max_steps, subagent_cmd_timeout=120)
    all_tasks = tb.load()
    if args.task_ids:
        want = {t.strip() for t in args.task_ids.split(",") if t.strip()}
        all_tasks = [t for t in all_tasks if t.task_id in want]
    elif args.tasks:
        all_tasks = all_tasks[: args.tasks]
    tasks = [t for t in all_tasks if t.task_id not in done_ids]

    bc = build(args.baseline)
    print(f"[setup] baseline={args.baseline}  mode={bc['mode']}  to-run={len(tasks)} "
          f"skipped={len(all_tasks)-len(tasks)}")

    t_start = time.time()
    n_pass = n_fail = 0
    cum = 0.0

    with out.open("a") as fout:
        for i, task in enumerate(tasks):
            t0 = time.time()
            print(f"\n[{i+1:>3}/{len(tasks)}] {task.task_id}", flush=True)
            record = {
                "task_id": task.task_id, "baseline": args.baseline,
                "reward": 0.0, "time_seconds": 0.0, "error": None,
            }
            try:
                if bc["mode"] == "flat":
                    res = tb.run_interactive(
                        task, bc["flat_router"],
                        worker_pool=None,
                        logs_dir=str(logs), flat_mode=True,
                    )
                else:
                    res = tb.run_hierarchical(
                        task,
                        planner_model=bc["planner_model"],
                        planner_api_base=bc["planner_api_base"],
                        planner_api_key=bc["planner_api_key"],
                        router_model=bc["router_model"],
                        router_api_base=bc["router_api_base"],
                        router_api_key=bc["router_api_key"],
                        pools=bc["pools"],
                        sub_model_api_base=bc["sub_model_api_base"],
                        sub_model_api_key=bc["sub_model_api_key"],
                        logs_dir=str(logs),
                        random_worker=bc["random_worker"],
                    )
                record["reward"] = float(res.reward or 0.0)
                record["error"] = res.error
            except Exception as e:
                record["error"] = f"runner crash: {e}"
                record["traceback"] = traceback.format_exc()[-500:]

            record["time_seconds"] = round(time.time() - t0, 1)
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            fout.flush()

            if record["reward"] >= 1.0:
                n_pass += 1
            else:
                n_fail += 1
            cum += record["reward"]
            n_done = n_pass + n_fail
            print(f"    reward={record['reward']:.2f} time={record['time_seconds']:.0f}s "
                  f"err={record['error'] or ''}")
            print(f"    running: pass={n_pass} fail={n_fail} rate={n_pass/max(1,n_done):.1%} "
                  f"elapsed={(time.time()-t_start)/60:.1f}min", flush=True)

    total = len(tasks)
    print(f"\n==== {args.baseline} summary ====")
    print(f"  tasks: {total}, pass: {n_pass}, fail: {n_fail}")
    print(f"  pass@1: {n_pass/max(1,total):.3%}")
    print(f"  avg reward: {cum/max(1,total):.3f}")
    print(f"  total time: {(time.time()-t_start)/60:.1f} min")
    print(f"  results: {out}")


if __name__ == "__main__":
    main()
