import json
import os
import queue
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from .config import DEFAULT_API_BASE, DEFAULT_LOCAL_BASE
from .routers import ROUTER_REGISTRY, BaseRouter
from .routers.local_router import LocalRouter
from .routers.direct import DirectRouter
from .benchmarks import BENCH_REGISTRY, BaseBenchmark
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


def _get_agent_prompt(bench_name: str) -> str:
    return AGENT_PROMPTS.get(bench_name, "")


def build_router(name: str, args) -> BaseRouter:
    kw = dict(api_base=args.api_base, api_key=args.api_key)
    if name == "planner":
        from .routers.planner_router import PlannerRouter
        return PlannerRouter(
            planner_model=args.local_model or "Qwen/Qwen2.5-7B-Instruct",
            router_model=getattr(args, 'router_model', None) or args.local_model or "Qwen/Qwen2.5-7B-Instruct",
            planner_api_base=args.local_base,
            router_api_base=args.local_base,
            sub_model_api_base=args.api_base,
            planner_api_key="EMPTY",
            router_api_key="EMPTY",
            sub_model_api_key=args.api_key,
        )
    if name == "local":
        return LocalRouter(
            local_base=args.local_base,
            model_name=args.local_model or "Qwen/Qwen2.5-7B-Instruct",
            agent_prompt=_get_agent_prompt(args.bench),
            **kw,
        )
    if name == "direct":
        if args.local_model:
            return DirectRouter(
                model_id=args.local_model,
                api_base=args.local_base,
                api_key="EMPTY",
            )
        return DirectRouter(model_id=args.direct_model or "gpt-5.4", **kw)
    if name.startswith("oracle-"):
        return ROUTER_REGISTRY[name](**kw)
    if name == "random":
        return ROUTER_REGISTRY["random"](**kw)
    if name == "uno-sft":
        from .routers.router_sft import UnoSFT
        return UnoSFT(
            local_base=args.local_base,
            model_name=args.local_model or "Uno-SFT",
            **kw,
        )
    raise ValueError(f"Unknown router: {name}")

def build_bench(name: str, args) -> BaseBenchmark:
    if name == "swebench":
        return BENCH_REGISTRY["swebench"](eval_workers=args.verify_workers)
    elif name == "terminalbench":
        return BENCH_REGISTRY["terminalbench"]()
    elif name in BENCH_REGISTRY:
        return BENCH_REGISTRY[name]()
    else:
        raise ValueError(f"Unknown benchmark: {name}. Available: {list(BENCH_REGISTRY)}")



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

    # Interactive mode: router ↔ Docker multi-turn (for both SWE-bench and Terminal-Bench)
    is_interactive = (hasattr(router, 'local') and hasattr(router, 'model_name')
                      and hasattr(bench, 'interactive_verify')
                      and args.interactive)

    # Pipeline mode: planner → router → sub-agent → Docker (our method)
    is_pipeline = (hasattr(bench, 'pipeline_verify') and getattr(args, 'pipeline', False))

    if is_pipeline:
        _run_pipeline_eval(bench, tasks, need_verify,
                           predictions, verification, pred_file, verify_file,
                           logs_dir, args)
    elif is_interactive:
        _run_interactive(router, bench, tasks, need_verify,
                         predictions, verification, pred_file, verify_file,
                         logs_dir, args)
    elif bench.name == "SWE-bench_Verified" and not args.interactive:
        _run_sequential(router, bench, tasks, need_gen, need_verify,
                        predictions, verification, pred_file, verify_file,
                        logs_dir, args)
    else:
        # TerminalBench one-shot: pipeline (generate + Docker verify concurrently)
        _run_pipelined(router, bench, tasks, need_gen, need_verify,
                       predictions, verification, pred_file, verify_file,
                       logs_dir, args)

    # ── Report ──
    total = len(predictions)
    passed = sum(1 for v in verification.values() if v.get("reward", 0) > 0)
    verified = len(verification)
    total_cost = sum(p.get("cost", 0) for p in predictions.values())
    model_usage = {}
    backend_usage = {}
    for p in predictions.values():
        for m in p.get("routed_models", []):
            model_usage[m] = model_usage.get(m, 0) + 1
        for b in p.get("routed_backends", []):
            backend_usage[b] = backend_usage.get(b, 0) + 1

    # Compute pass@k from stored per-attempt rewards
    pass_at = {}
    for k in [1, 3]:
        count = 0
        for v in verification.values():
            attempts = v.get("pass_at_k", [v.get("reward", 0)])
            count += int(any(r > 0 for r in attempts[:k]))
        pass_at[k] = round(count / max(verified, 1), 4)

    summary = {
        "router": router.name, "benchmark": bench.name,
        "total": total, "verified": verified, "passed": passed,
        "pass_at_1": pass_at.get(1, 0), "pass_at_3": pass_at.get(3, 0),
        "passed_ids": [tid for tid, v in verification.items() if v.get("reward", 0) > 0],
        "total_cost_usd": round(total_cost, 4),
        "avg_cost": round(total_cost / max(total, 1), 6),
        "avg_routes": round(sum(p.get("route_count", 0) for p in predictions.values()) / max(total, 1), 2),
        "model_usage": model_usage,
        "backend_usage": backend_usage,
    }
    with open(out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"RESULTS — {router.name} on {bench.name}")
    print(f"  Pass@1: {pass_at.get(1,0)*100:.1f}%  Pass@3: {pass_at.get(3,0)*100:.1f}%  ({passed}/{verified})")
    print(f"  Cost: ${total_cost:.4f} (${total_cost/max(total,1):.6f}/task)")
    if backend_usage:
        print(f"  Backends: {backend_usage}")
    print(f"  Output: {out}")
    print(f"{'='*60}")
    return summary


def _run_interactive(router, bench, tasks, need_verify,
                     predictions, verification, pred_file, verify_file,
                     logs_dir, args):
    """Interactive mode: router ↔ Docker multi-turn for each task.
    Supports pass@k: run up to k attempts, pass if any succeeds."""
    pass_k = getattr(args, 'pass_k', 1)
    to_run = [t for t in tasks if t.task_id not in verification]
    if not to_run:
        print("Interactive: all tasks already verified")
        return

    print(f"Interactive mode: {len(to_run)} tasks, pass@{pass_k} (workers={args.verify_workers})")

    def _run_one_task(task):
        """Run up to pass_k attempts for a single task."""
        rewards = []
        for attempt in range(pass_k):
            attempt_logs = os.path.join(logs_dir, f"attempt_{attempt}") if pass_k > 1 else logs_dir
            vr = bench.interactive_verify(task, router, attempt_logs)
            rewards.append(vr.reward)
            if vr.reward > 0:
                break  # early stop on first success
        best_reward = max(rewards)
        return task, best_reward, rewards, vr

    lock = threading.Lock()
    with open(verify_file, "a") as vout, open(pred_file, "a") as pout:
        with ThreadPoolExecutor(max_workers=args.verify_workers) as ex:
            futs = {ex.submit(_run_one_task, t): t for t in to_run}
            for fut in tqdm(as_completed(futs), total=len(to_run), desc="Interactive"):
                task, best_reward, rewards, last_vr = fut.result()
                d = {"task_id": task.task_id, "reward": best_reward,
                     "pass_at_k": rewards,
                     "error": last_vr.error, "log": last_vr.log[:500]}
                pred_entry = {"task_id": task.task_id, "answer": "(interactive)",
                              "route_count": 0, "routed_models": [], "cost": 0}
                with lock:
                    verification[task.task_id] = d
                    vout.write(json.dumps(d) + "\n")
                    vout.flush()
                    if task.task_id not in predictions:
                        predictions[task.task_id] = pred_entry
                        pout.write(json.dumps(pred_entry) + "\n")
                        pout.flush()
                status = "PASS" if vr.reward > 0 else "FAIL"
                err = f" ({vr.error})" if vr.error else ""
                tqdm.write(f"  {vr.task_id}: {status}{err}")


def _run_pipeline_eval(bench, tasks, need_verify,
                       predictions, verification, pred_file, verify_file,
                       logs_dir, args):
    """Pipeline mode: planner → router → sub-agent → Docker verify.
    Supports pass@k: run up to k attempts per task, pass if any succeeds."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from configs import load_pools

    pools = load_pools()
    pass_k = getattr(args, 'pass_k', 1)
    to_run = [t for t in tasks if t.task_id not in verification]
    if not to_run:
        print("Pipeline: all tasks already verified")
        return

    print(f"Pipeline mode: {len(to_run)} tasks, pass@{pass_k} (workers={args.verify_workers})")

    def _run_one_task(task):
        """Run up to pass_k attempts for a single task."""
        rewards = []
        last_vr = None
        for attempt in range(pass_k):
            attempt_logs = os.path.join(logs_dir, f"attempt_{attempt}") if pass_k > 1 else logs_dir
            vr = bench.pipeline_verify(task, args, pools, attempt_logs)
            rewards.append(vr.reward)
            last_vr = vr
            if vr.reward > 0:
                break  # early stop on first success
        best_reward = max(rewards)
        return task, best_reward, rewards, last_vr

    lock = threading.Lock()
    with open(verify_file, "a") as vout, open(pred_file, "a") as pout:
        with ThreadPoolExecutor(max_workers=args.verify_workers) as ex:
            futs = {ex.submit(_run_one_task, t): t for t in to_run}
            for fut in tqdm(as_completed(futs), total=len(to_run), desc="Pipeline"):
                task, best_reward, rewards, last_vr = fut.result()
                vr = last_vr
                d = {"task_id": vr.task_id, "reward": best_reward,
                     "pass_at_k": rewards,
                     "error": vr.error, "log": vr.log[:500]}
                pred_entry = {"task_id": vr.task_id, "answer": "(pipeline)",
                              "route_count": 0, "routed_models": [], "cost": 0}
                with lock:
                    verification[vr.task_id] = d
                    vout.write(json.dumps(d) + "\n")
                    vout.flush()
                    if vr.task_id not in predictions:
                        predictions[vr.task_id] = pred_entry
                        pout.write(json.dumps(pred_entry) + "\n")
                        pout.flush()
                status = "PASS" if vr.reward > 0 else "FAIL"
                err = f" ({vr.error})" if vr.error else ""
                tqdm.write(f"  {vr.task_id}: {status}{err}")


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
        "routed_backends": res.routed_backends,
        "cost": res.total_cost, "tokens": res.total_tokens,
    }

def main():
    parser = argparse.ArgumentParser(description="Eval pipeline: router → sub-agent → benchmark")
    parser.add_argument("--router", required=True, choices=list(ROUTER_REGISTRY))
    parser.add_argument("--bench", required=True, choices=list(BENCH_REGISTRY),
                        help="Benchmark to evaluate. Use comma-separated for multiple: gpqa,mmlu,math500")
    parser.add_argument("--api_key", required=True)
    parser.add_argument("--api_base", default=DEFAULT_API_BASE)
    parser.add_argument("--local_base", default=DEFAULT_LOCAL_BASE)
    parser.add_argument("--local_model", default=None)
    parser.add_argument("--direct_model", default=None)
    parser.add_argument("--router_model", default=None,
                        help="Router model (for planner mode, if different from planner)")
    parser.add_argument("--output_dir", default=None)
    parser.add_argument("--max_tasks", type=int, default=None)
    parser.add_argument("--gen_workers", type=int, default=16)
    parser.add_argument("--verify_workers", type=int, default=4)
    parser.add_argument("--skip_gen", action="store_true")
    parser.add_argument("--skip_verify", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--pipeline", action="store_true", help="Pipeline mode: planner→router→sub-agent→Docker")
    parser.add_argument("--pass-k", type=int, default=1, help="pass@k: run up to k attempts per task")
    parser.add_argument("--worker_mode",
                        choices=["backbone", "random", "learned", "strongest"],
                        default="learned",
                        help="How each subtask's sub-agent model is chosen. "
                             "backbone=local LLM, random=uniform(pool), "
                             "learned=router LLM decides, strongest=fixed --strongest_model")
    parser.add_argument("--strongest_model", default="claude-opus-4-6",
                        help="Model id used when --worker_mode=strongest")
    args = parser.parse_args()
    args.pass_k = args.pass_k  # ensure accessible

    if not args.output_dir:
        args.output_dir = f"/data/xieht/eval_results/{args.router}_{args.bench}"

    router = build_router(args.router, args)
    bench = build_bench(args.bench, args)
    run_pipeline(router, bench, args)
