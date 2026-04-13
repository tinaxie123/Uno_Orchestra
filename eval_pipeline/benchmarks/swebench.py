"""
SWE-bench Verified benchmark adapter.
Supports both single-task Docker verification (for pipeline mode)
and batch verification via swebench harness.
"""
import re
import os
import json
import subprocess
import tempfile
import time
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


class SWEBench(BaseBenchmark):

    def __init__(self, dataset="princeton-nlp/SWE-bench_Verified", split="test",
                 conda_env="swebench", eval_timeout=900, eval_workers=4):
        self.dataset = dataset
        self.split = split
        self.conda_env = conda_env
        self.eval_timeout = eval_timeout
        self.eval_workers = eval_workers

    @property
    def name(self):
        return "SWE-bench_Verified"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset(self.dataset, split=self.split)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))
        tasks = []
        for inst in ds:
            problem = inst["problem_statement"]
            question = (
                f"Bug report for Python repository {inst['repo']}:\n\n"
                f"{problem[:4000]}\n\n"
                f"Provide a minimal unified diff patch that fixes this bug."
            )
            tasks.append(Task(
                task_id=inst["instance_id"], raw=inst, question=question,
                context={"repo": inst["repo"], "problem_statement": problem},
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        m = re.search(r"```(?:diff)?\s*\n((?:---|\+\+\+|diff\s).*?)```", router_output, re.DOTALL)
        if m:
            return m.group(1).strip()
        m = re.search(r"((?:---\s+a/|diff\s+--git\s).*?)(?:\n\n|\Z)", router_output, re.DOTALL)
        if m:
            return m.group(1).strip()
        return router_output

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        """
        Single-task verification: write a 1-line predictions.jsonl,
        call swebench harness with --instance_ids, parse result.
        """
        work_dir = os.path.join(logs_dir or "/tmp", task.task_id.replace("/", "_"))
        os.makedirs(work_dir, exist_ok=True)
        pred_path = os.path.join(work_dir, "predictions.jsonl")
        run_id = f"single_{int(time.time())}"

        # Write single prediction
        with open(pred_path, "w") as f:
            f.write(json.dumps({
                "instance_id": task.task_id,
                "model_name_or_path": "eval",
                "model_patch": answer,
            }) + "\n")

        # Run harness for this single instance
        cmd = [
            "conda", "run", "-n", self.conda_env, "--no-banner",
            "python3", "-m", "swebench.harness.run_evaluation",
            "--dataset_name", self.dataset, "--split", self.split,
            "--predictions_path", pred_path,
            "--instance_ids", task.task_id,
            "--max_workers", "1",
            "--run_id", run_id,
            "--timeout", str(self.eval_timeout),
            "--cache_level", "instance",
            "--report_dir", os.path.join(work_dir, "reports"),
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.eval_timeout + 300)
            log = f"stdout: {proc.stdout[-1000:]}\nstderr: {proc.stderr[-1000:]}"
        except subprocess.TimeoutExpired:
            return VerifyResult(task.task_id, 0.0, error="Harness timeout")
        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300])

        # Parse report
        resolved = False
        report_dir = os.path.join(work_dir, "reports", run_id)
        for root, dirs, files in os.walk(report_dir):
            for fname in files:
                if fname.endswith(".json"):
                    try:
                        data = json.load(open(os.path.join(root, fname)))
                        if task.task_id in data.get("resolved", []):
                            resolved = True
                    except Exception:
                        pass

        return VerifyResult(task.task_id, 1.0 if resolved else 0.0, log=log[:500])

    def verify_batch(self, tasks: List[Task], answers: List[str],
                     logs_dir: str = None) -> List[VerifyResult]:
        """Batch verification via swebench harness (legacy, still available)."""
        work_dir = logs_dir or tempfile.mkdtemp(prefix="swebench_eval_")
        os.makedirs(work_dir, exist_ok=True)
        pred_path = os.path.join(work_dir, "predictions.jsonl")
        run_id = "eval_run"

        with open(pred_path, "w") as f:
            for task, ans in zip(tasks, answers):
                f.write(json.dumps({
                    "instance_id": task.task_id,
                    "model_name_or_path": "eval",
                    "model_patch": ans,
                }) + "\n")

        cmd = [
            "conda", "run", "-n", self.conda_env, "--no-banner",
            "python3", "-m", "swebench.harness.run_evaluation",
            "--dataset_name", self.dataset, "--split", self.split,
            "--predictions_path", pred_path,
            "--max_workers", str(self.eval_workers),
            "--run_id", run_id,
            "--timeout", str(self.eval_timeout),
            "--cache_level", "instance",
            "--report_dir", os.path.join(work_dir, "reports"),
        ]
        print(f"[SWE-bench] Running harness: {' '.join(cmd[:8])}...")
        subprocess.run(cmd, timeout=7200)

        resolved_ids = set()
        report_dir = os.path.join(work_dir, "reports", run_id)
        for root, dirs, files in os.walk(report_dir):
            for fname in files:
                if fname.endswith(".json"):
                    try:
                        data = json.load(open(os.path.join(root, fname)))
                        if "resolved" in data:
                            resolved_ids.update(data["resolved"])
                    except Exception:
                        pass

        return [VerifyResult(t.task_id, 1.0 if t.task_id in resolved_ids else 0.0) for t in tasks]
