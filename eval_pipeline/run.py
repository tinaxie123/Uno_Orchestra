"""
Unified eval pipeline for all router models on all benchmarks.
Supports pipelined mode: generate + Docker verify concurrently.

Usage:
    # Router-R1 on Terminal-Bench (pipelined: generate + verify concurrently)
    python -m eval_pipeline.run --router router-r1 --bench terminalbench --api_key KEY

    # Router-R1 on SWE-bench
    python -m eval_pipeline.run --router router-r1 --bench swebench --api_key KEY

    # Baselines
    python -m eval_pipeline.run --router oracle-strongest --bench swebench --api_key KEY
    python -m eval_pipeline.run --router random --bench terminalbench --api_key KEY
"""
import json
import queue
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from .config import DEFAULT_API_BASE, DEFAULT_LOCAL_BASE
from .routers import ROUTER_REGISTRY, BaseRouter
from .routers.router_r1 import RouterR1
from .routers.direct import DirectRouter
from .benchmarks import BENCH_REGISTRY, BaseBenchmark, Task

# ═══════════════════════════════════════════════════════════════════════
# Sub-agent prompts per benchmark
# ═══════════════════════════════════════════════════════════════════════

AGENT_PROMPTS = {
    "swebench": (
        "You are a software engineering expert.\n"
        "Repository: {repo}\nBug (truncated): {problem_statement:.3000}\n"
        "Sub-question: {query}\n\n"
        "Provide a minimal unified diff patch. Output ONLY ```diff ... ```."
    ),
    "terminalbench": (
        "You are a systems programming expert.\n"
        "Task: {task_instruction:.3000}\n"
        "Sub-question: {query}\n\n"
        "Provide complete executable bash commands. Use ```bash ... ``` blocks."
    ),
}


def build_router(name: str, args) -> BaseRouter:
    kw = dict(api_base=args.api_base, api_key=args.api_key)
    if name == "router-r1":
        return RouterR1(local_base=args.local_base,
                        agent_prompt=AGENT_PROMPTS.get(args.bench, ""), **kw)
    elif name == "skillrouter-sft":
        from .routers.skillrouter_sft import SkillRouterSFT
        return SkillRouterSFT(local_base=args.local_base,
                              model_name=args.local_model or "SkillRouter-SFT", **kw)
    elif name == "direct":
        # If local_model specified, use local vLLM; else use API
        if args.local_model:
            return DirectRouter(model_id=args.local_model,
                                api_base=args.local_base, api_key="EMPTY")
        return DirectRouter(model_id=args.direct_model or "gpt-5.4", **kw)
    elif name.startswith("oracle-"):
        return ROUTER_REGISTRY[name](**kw)
    elif name == "random":
        return ROUTER_REGISTRY["random"](**kw)
    else:
        raise ValueError(f"Unknown router: {name}")


def build_bench(name: str, args) -> BaseBenchmark:
    if name == "swebench":
        return BENCH_REGISTRY["swebench"](eval_workers=args.verify_workers)
    elif name == "terminalbench":
        return BENCH_REGISTRY["terminalbench"]()
    else:
        raise ValueError(f"Unknown benchmark: {name}")


# ═══════════════════════════════════════════════════════════════════════
# Pipelined execution: generate + verify concurrently
# ═══════════════════════════════════════════════════════════════════════

def run_pipeline(router: BaseRouter, bench: BaseBenchmark, args):
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pred_file = out / "predictions.jsonl"
    verify_file = out / "verification.jsonl"
    logs_dir = str(out / "logs")

    print(f"{'='*60}")
    print(f"Router: {router.name}  |  Benchmark: {bench.name}")
    print(f"Output: {out}")
    print(f"{'='*60}")

    # Load tasks
    tasks = bench.load(args.max_tasks)
    task_map = {t.task_id: t for t in tasks}

    # Resume cached predictions
    predictions = {}
    if pred_file.exists():
        with open(pred_file) as f:
            for line in f:
                r = json.loads(line)
                predictions[r["task_id"]] = r

    # Resume cached verifications
    verification = {}
    if verify_file.exists():
        with open(verify_file) as f:
            for line in f:
                r = json.loads(line)
                verification[r["task_id"]] = r

    print(f"Cached: {len(predictions)} predictions, {len(verification)} verifications")

    # Determine what needs to be done
    need_gen = [t for t in tasks if t.task_id not in predictions]
    need_verify = [t for t in tasks if t.task_id not in verification]

    if bench.name == "SWE-bench_Verified":
        # SWE-bench: generate all → batch harness verification (official method)
        _run_sequential(router, bench, tasks, need_gen, need_verify,
                        predictions, verification, pred_file, verify_file,
                        logs_dir, args)
    else:
        # TerminalBench: pipeline (generate + Docker verify concurrently)
        _run_pipelined(router, bench, tasks, need_gen, need_verify,
                       predictions, verification, pred_file, verify_file,
                       logs_dir, args)

    # ── Report ──
    total = len(predictions)
    passed = sum(1 for v in verification.values() if v.get("reward", 0) > 0)
    verified = len(verification)
    total_cost = sum(p.get("cost", 0) for p in predictions.values())
    model_usage = {}
    for p in predictions.values():
        for m in p.get("routed_models", []):
            model_usage[m] = model_usage.get(m, 0) + 1

    summary = {
        "router": router.name, "benchmark": bench.name,
        "total": total, "verified": verified, "passed": passed,
        "pass_rate": round(passed / max(verified, 1), 4),
        "passed_ids": [tid for tid, v in verification.items() if v.get("reward", 0) > 0],
        "total_cost_usd": round(total_cost, 4),
        "avg_cost": round(total_cost / max(total, 1), 6),
        "avg_routes": round(sum(p.get("route_count", 0) for p in predictions.values()) / max(total, 1), 2),
        "model_usage": model_usage,
    }
    with open(out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"RESULTS — {router.name} on {bench.name}")
    print(f"  Pass rate: {passed}/{verified} ({passed/max(verified,1)*100:.1f}%)")
    print(f"  Cost: ${total_cost:.4f} (${total_cost/max(total,1):.6f}/task)")
    print(f"  Routing: avg {summary['avg_routes']} routes, models={model_usage}")
    print(f"  Output: {out}")
    print(f"{'='*60}")
    return summary


def _run_sequential(router, bench, tasks, need_gen, need_verify,
                    predictions, verification, pred_file, verify_file,
                    logs_dir, args):
    """Sequential: generate all → verify all (for SWE-bench batch harness)."""
    # Generate
    if need_gen and not args.skip_gen:
        print(f"Generating {len(need_gen)} predictions (workers={args.gen_workers})...")
        lock = threading.Lock()
        with open(pred_file, "a") as fout:
            with ThreadPoolExecutor(max_workers=args.gen_workers) as ex:
                futs = {ex.submit(_gen_one, router, bench, t): t for t in need_gen}
                for fut in tqdm(as_completed(futs), total=len(need_gen), desc="Generating"):
                    entry = fut.result()
                    with lock:
                        predictions[entry["task_id"]] = entry
                        fout.write(json.dumps(entry) + "\n")
                        fout.flush()

    # Verify
    if need_verify and not args.skip_verify:
        ordered = [t for t in tasks if t.task_id in predictions and t.task_id not in verification]
        answers = [predictions[t.task_id]["answer"] for t in ordered]
        if ordered:
            print(f"Verifying {len(ordered)} tasks via swebench harness...")
            results = bench.verify_batch(ordered, answers, logs_dir)
            with open(verify_file, "a") as fout:
                for vr in results:
                    d = {"task_id": vr.task_id, "reward": vr.reward,
                         "error": vr.error, "log": vr.log[:500]}
                    verification[vr.task_id] = d
                    fout.write(json.dumps(d) + "\n")


def _run_pipelined(router, bench, tasks, need_gen, need_verify,
                   predictions, verification, pred_file, verify_file,
                   logs_dir, args):
    """
    Pipelined: generate + verify concurrently via producer-consumer queue.
    Generator threads → queue → verifier threads.
    Already-generated tasks go straight to verification.
    """
    verify_queue = queue.Queue()
    pred_lock = threading.Lock()
    verify_lock = threading.Lock()
    gen_done = threading.Event()

    # Enqueue already-generated but unverified tasks immediately
    already_gen_unverified = [t for t in tasks
                              if t.task_id in predictions and t.task_id not in verification]
    for t in already_gen_unverified:
        verify_queue.put((t, predictions[t.task_id]["answer"]))
    print(f"Pipeline: {len(already_gen_unverified)} ready for verification, "
          f"{len(need_gen)} need generation")

    total_to_verify = len(already_gen_unverified) + len(need_gen)
    pbar = tqdm(total=total_to_verify, desc="Pipeline (gen+verify)")

    # ── Generator thread ──
    def generator():
        if args.skip_gen or not need_gen:
            gen_done.set()
            return
        with open(pred_file, "a") as fout:
            with ThreadPoolExecutor(max_workers=args.gen_workers) as ex:
                futs = {ex.submit(_gen_one, router, bench, t): t for t in need_gen}
                for fut in as_completed(futs):
                    entry = fut.result()
                    task = futs[fut]
                    with pred_lock:
                        predictions[entry["task_id"]] = entry
                        fout.write(json.dumps(entry) + "\n")
                        fout.flush()
                    # Push to verify queue immediately
                    if task.task_id not in verification:
                        verify_queue.put((task, entry["answer"]))
        gen_done.set()

    # ── Verifier thread ──
    def verifier():
        with open(verify_file, "a") as fout:
            with ThreadPoolExecutor(max_workers=args.verify_workers) as ex:
                pending = {}
                while True:
                    # Drain queue
                    while True:
                        try:
                            task, answer = verify_queue.get_nowait()
                            fut = ex.submit(bench.verify, task, answer, logs_dir)
                            pending[fut] = task
                        except queue.Empty:
                            break

                    # Check completed futures
                    done_futs = [f for f in pending if f.done()]
                    for fut in done_futs:
                        task = pending.pop(fut)
                        vr = fut.result()
                        d = {"task_id": vr.task_id, "reward": vr.reward,
                             "error": vr.error, "log": vr.log[:500]}
                        with verify_lock:
                            verification[vr.task_id] = d
                            fout.write(json.dumps(d) + "\n")
                            fout.flush()
                        status = "PASS" if vr.reward > 0 else "FAIL"
                        err = f" ({vr.error})" if vr.error else ""
                        pbar.update(1)
                        pbar.set_postfix_str(f"{task.task_id}: {status}{err}")

                    # Exit condition: generator done + queue empty + no pending
                    if gen_done.is_set() and verify_queue.empty() and not pending:
                        break

                    # Small sleep to avoid busy-wait
                    if not done_futs and verify_queue.empty():
                        threading.Event().wait(0.5)

    # Launch both
    gen_thread = threading.Thread(target=generator, name="generator")
    ver_thread = threading.Thread(target=verifier, name="verifier")
    gen_thread.start()
    ver_thread.start()
    gen_thread.join()
    ver_thread.join()
    pbar.close()


def _gen_one(router, bench, task):
    """Generate one prediction."""
    res = router.route(task.question, task.context)
    answer = bench.extract_answer(res.answer, task)
    return {
        "task_id": task.task_id, "answer": answer,
        "full_trace": res.full_trace, "route_count": res.route_count,
        "routed_models": res.routed_models, "routed_skills": res.routed_skills,
        "cost": res.total_cost, "tokens": res.total_tokens,
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Unified router evaluation pipeline")
    parser.add_argument("--router", required=True, choices=list(ROUTER_REGISTRY))
    parser.add_argument("--bench", required=True, choices=list(BENCH_REGISTRY))
    parser.add_argument("--api_key", required=True)
    parser.add_argument("--api_base", default=DEFAULT_API_BASE)
    parser.add_argument("--local_base", default=DEFAULT_LOCAL_BASE)
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--max_tasks", type=int, default=None)
    parser.add_argument("--gen_workers", type=int, default=16)
    parser.add_argument("--verify_workers", type=int, default=4)
    parser.add_argument("--skip_gen", action="store_true")
    parser.add_argument("--skip_verify", action="store_true")
    parser.add_argument("--direct_model", default=None, help="API model for direct router")
    parser.add_argument("--local_model", default=None, help="Local vLLM model name")
    args = parser.parse_args()

    if not args.output_dir:
        args.output_dir = f"/data/xieht/eval_results/{args.router}_{args.bench}"

    router = build_router(args.router, args)
    bench = build_bench(args.bench, args)
    run_pipeline(router, bench, args)


if __name__ == "__main__":
    main()
