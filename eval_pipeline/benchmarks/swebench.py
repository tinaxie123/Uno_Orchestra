"""
SWE-bench Verified benchmark adapter.

Two modes (mirrors AOrchestra design):
1. One-shot: router generates patch blindly → swebench harness verifies (baseline, weak)
2. Interactive: router ↔ Docker multi-turn — real shell feedback each step (correct approach)
"""
import re
import os
import json
import subprocess
import tempfile
import time
from typing import List, Tuple, Dict, Optional
from .base import BaseBenchmark, Task, VerifyResult

# ── Test evaluation helpers (from AOrchestra/swebench_executor.py) ──

START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"

REPO_TEST_CMDS = {
    "astropy/astropy": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "django/django": "./tests/runtests.py --verbosity 2 {tests}",
    "matplotlib/matplotlib": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pallets/flask": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "psf/requests": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pylint-dev/pylint": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "pytest-dev/pytest": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "scikit-learn/scikit-learn": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "sphinx-doc/sphinx": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
    "sympy/sympy": "bin/test -C --verbose {tests}",
    "pydata/xarray": "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider",
}
DEFAULT_TEST_CMD = "python -m pytest {tests} --no-header -rA --tb=no -p no:cacheprovider"


def _make_eval_script(repo, base_commit, test_patch, repo_dir="/testbed", env_name="testbed"):
    """Generate test evaluation script (from AOrchestra)."""
    HEREDOC = "EOF_114329324912"
    test_files = re.findall(r"diff --git a/.* b/(.*)", test_patch) if test_patch else []
    directives = [f for f in test_files if not any(f.endswith(e) for e in
                  [".json",".png",".jpg",".txt",".md",".rst",".yaml",".yml",".toml",".cfg",".lock"])]
    if repo == "django/django":
        directives = [d[len("tests/"):].replace("/",".").rstrip(".py") if d.startswith("tests/") else d for d in directives]
    test_cmd = REPO_TEST_CMDS.get(repo, DEFAULT_TEST_CMD).format(tests=" ".join(directives))
    reset = f"git checkout {base_commit} -- {' '.join(test_files)}" if test_files else ":"
    apply_patch = f"git apply -v - <<'{HEREDOC}'\n{test_patch}\n{HEREDOC}" if test_patch else ":"
    return "\n".join([
        "#!/bin/bash", "set -uxo pipefail", "",
        "source /opt/miniconda3/bin/activate", f"conda activate {env_name}",
        f"cd {repo_dir}", f"git config --global --add safe.directory {repo_dir}",
        "git status", f"git -c core.fileMode=false diff {base_commit}", "",
        "source /opt/miniconda3/bin/activate", f"conda activate {env_name}",
        reset, "", apply_patch, "",
        f": '{START_TEST_OUTPUT}'", test_cmd, f": '{END_TEST_OUTPUT}'", "",
        reset,
    ]) + "\n"


def _parse_test_results(test_output, repo, fail_to_pass, pass_to_pass):
    """Parse test output → reward (from AOrchestra)."""
    start = test_output.find(START_TEST_OUTPUT)
    end = test_output.find(END_TEST_OUTPUT)
    log = test_output[start:end] if start != -1 and end != -1 else test_output

    # Parse pytest/django output
    status = {}
    if repo == "django/django":
        for m in re.finditer(r"^(test_\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR)", log, re.M):
            status[f"{m.group(1)} ({m.group(2)})"] = "PASSED" if m.group(3) == "ok" else "FAILED"
    else:
        for m in re.finditer(r"^(.*?)\s+(PASSED|FAILED|ERROR)", log, re.M):
            status[m.group(1).strip()] = m.group(2)

    def _check(test_name, want_pass=True):
        for k, s in status.items():
            if test_name in k or k in test_name:
                return (s == "PASSED") == want_pass
            method = test_name.split("::")[-1] if "::" in test_name else test_name.split(".")[-1]
            if method in k:
                return (s == "PASSED") == want_pass
        return want_pass  # optimistic default

    f2p_ok = all(_check(t, True) for t in (fail_to_pass or []))
    p2p_ok = all(_check(t, True) for t in (pass_to_pass or []))
    return 1.0 if (f2p_ok and p2p_ok) else 0.0


# ── Docker helpers ──

def _docker_exec(container, cmd, timeout=120):
    """Execute command in Docker container, return (stdout, exit_code)."""
    try:
        p = subprocess.run(
            ["docker", "exec", "-i", container, "bash"],
            input=cmd.encode(), capture_output=True, timeout=timeout,
        )
        out = p.stdout.decode("utf-8", errors="replace")
        if p.stderr:
            out += "\nSTDERR: " + p.stderr.decode("utf-8", errors="replace")[-500:]
        return out[-3000:], p.returncode
    except subprocess.TimeoutExpired:
        return "[timeout]", -1
    except Exception as e:
        return f"[error: {e}]", -1


def _get_swebench_image(instance_id):
    """Map instance_id → Docker image name (swebench convention)."""
    parts = instance_id.split("__")
    owner = parts[0]
    repo_issue = parts[1] if len(parts) > 1 else instance_id
    return f"swebench/sweb.eval.x86_64.{owner}_1776_{repo_issue}"


# ── SWE-bench system prompt for interactive mode ──

SWEBENCH_INTERACTIVE_PROMPT = """\
You are a software engineer fixing a GitHub issue inside a Docker container.
The repository is at /testbed, already checked out to the correct commit.

Available actions (respond with EXACTLY one per turn):

DISCUSSION
<your reasoning about what to do next>
COMMAND
<single bash command to execute>

OR when you're done fixing:

DISCUSSION
<explain your fix>
COMMAND
submit

Commands run in /testbed with conda env "testbed" active.
Useful commands: find, grep -rn, cat, python, pytest, git diff, etc.

IMPORTANT:
- Activate conda first: source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed
- Explore before editing. Use grep/find to locate relevant code.
- Make minimal, targeted changes.
- Test your fix before submitting.
"""


class SWEBench(BaseBenchmark):

    def __init__(self, dataset="princeton-nlp/SWE-bench_Verified", split="test",
                 conda_env="swebench", eval_timeout=900, eval_workers=4,
                 max_steps=30, docker_timeout=1800):
        self.dataset = dataset
        self.split = split
        self.conda_env = conda_env
        self.eval_timeout = eval_timeout
        self.eval_workers = eval_workers
        self.max_steps = max_steps
        self.docker_timeout = docker_timeout

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
            # Parse FAIL_TO_PASS / PASS_TO_PASS (may be JSON strings)
            f2p = inst.get("FAIL_TO_PASS", [])
            p2p = inst.get("PASS_TO_PASS", [])
            if isinstance(f2p, str):
                try: f2p = json.loads(f2p)
                except: f2p = []
            if isinstance(p2p, str):
                try: p2p = json.loads(p2p)
                except: p2p = []
            tasks.append(Task(
                task_id=inst["instance_id"], raw=inst,
                question=f"Fix this issue in {inst['repo']}:\n\n{problem[:4000]}",
                context={
                    "repo": inst["repo"],
                    "problem_statement": problem,
                    "base_commit": inst["base_commit"],
                    "test_patch": inst.get("test_patch", ""),
                    "hints_text": inst.get("hints_text", ""),
                    "FAIL_TO_PASS": f2p,
                    "PASS_TO_PASS": p2p,
                },
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

    # ─── Interactive mode: router ↔ Docker multi-turn (AOrchestra style) ───

    def interactive_verify(self, task: Task, router, logs_dir=None) -> VerifyResult:
        """
        Multi-turn interactive evaluation (following AOrchestra):
        1. Start swebench Docker container with repo at base_commit
        2. Router sees issue → DISCUSSION + COMMAND
        3. COMMAND executes in Docker → real output back to router
        4. Repeat until 'submit' or max_steps
        5. Run official test eval script inside container
        """
        from ..config import COST_PER_M, EVAL_MAX_TOKENS, SUB_AGENT_TEMP, resolve_model

        ctx = task.context
        image = _get_swebench_image(task.task_id)
        container = f"swebench_{task.task_id.replace('/', '_')}_{int(time.time())}"
        task_logs = os.path.join(logs_dir or "/tmp", task.task_id.replace("/", "_"))
        os.makedirs(task_logs, exist_ok=True)
        log = ""
        total_cost = 0.0

        # Regex to parse agent output
        CMD_RE = re.compile(r"COMMAND\s*\n(.+?)(?:\n\n|\Z)", re.DOTALL)

        try:
            # ── Start container ──
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
            rc = subprocess.run([
                "docker", "run", "-d", "--name", container,
                image, "tail", "-f", "/dev/null",
            ], capture_output=True, text=True, timeout=120)
            if rc.returncode != 0:
                return VerifyResult(task.task_id, 0.0, error=f"docker run: {rc.stderr[:300]}")

            # Checkout base commit
            _docker_exec(container, f"cd /testbed && git checkout -f {ctx['base_commit']}", timeout=60)
            _docker_exec(container, "cd /testbed && git reset --hard HEAD && git clean -fd", timeout=30)

            # ── Build initial prompt ──
            instruction = (
                f"## Repository: {ctx['repo']}\n\n"
                f"## Issue\n{ctx['problem_statement'][:6000]}\n"
            )
            if ctx.get("hints_text"):
                instruction += f"\n## Hints\n{ctx['hints_text'][:2000]}\n"

            messages = [
                {"role": "system", "content": SWEBENCH_INTERACTIVE_PROMPT},
                {"role": "user", "content": instruction},
            ]

            # ── Multi-turn loop ──
            for step in range(self.max_steps):
                # Get router decision
                try:
                    resp = router.local.chat.completions.create(
                        model=router.model_name,
                        messages=messages,
                        temperature=0.0,
                        max_tokens=2048,
                    )
                    assistant_text = resp.choices[0].message.content or ""
                except Exception as e:
                    log += f"\n[ROUTER ERROR step {step}: {e}]"
                    break

                messages.append({"role": "assistant", "content": assistant_text})
                log += f"\n[STEP {step+1}] ASSISTANT:\n{assistant_text[:500]}\n"

                # Parse COMMAND
                cmd_match = CMD_RE.search(assistant_text)
                if not cmd_match:
                    # No command found, maybe router is done or confused
                    log += "[NO COMMAND FOUND]\n"
                    break

                command = cmd_match.group(1).strip().split("\n")[0].strip()  # first line only

                # ── Submit: run tests ──
                if command.lower() == "submit":
                    log += "[SUBMIT]\n"
                    break

                # ── Execute command in Docker ──
                # Wrap with cd /testbed for convenience
                exec_cmd = f"cd /testbed && {command}"
                output, exit_code = _docker_exec(container, exec_cmd, timeout=120)

                obs = f"[Step {step+1}/{self.max_steps}] exit_code={exit_code}\n{output}"
                log += f"[STEP {step+1}] CMD: {command}\n[OUTPUT] {output[:500]}\n"

                # Also check if router wants to call API sub-agent via <search>
                search_m = re.search(r"<search>(.*?)(?:</search>|$)", assistant_text, re.DOTALL)
                if search_m:
                    raw = search_m.group(1).strip()
                    parts = raw.split(":", 1)
                    from ..routers.local_router import _resolve, DEFAULT_MODEL
                    mid = _resolve(parts[0]) if len(parts) > 1 else DEFAULT_MODEL
                    query = parts[1].strip() if len(parts) > 1 else raw
                    try:
                        sr = router.api.chat.completions.create(
                            model=mid,
                            messages=[{"role": "user", "content": f"SWE-bench issue in {ctx['repo']}:\n{ctx['problem_statement'][:3000]}\n\nQuestion: {query}"}],
                            temperature=SUB_AGENT_TEMP, max_tokens=EVAL_MAX_TOKENS,
                        )
                        api_txt = sr.choices[0].message.content or ""
                        toks = getattr(sr.usage, "completion_tokens", 0) or 0
                        total_cost += COST_PER_M.get(mid, 10.0) * max(toks, 1) / 1e6
                        obs += f"\n[API {mid}]: {api_txt[:1500]}"
                    except Exception as e:
                        obs += f"\n[API ERROR: {e}]"

                messages.append({"role": "user", "content": obs})

            # ── Run test evaluation ──
            eval_script = _make_eval_script(
                repo=ctx["repo"],
                base_commit=ctx["base_commit"],
                test_patch=ctx.get("test_patch", ""),
            )
            # Write and execute eval script
            _docker_exec(container, f"cat > /eval.sh << 'EOF_EVAL'\n{eval_script}\nEOF_EVAL", timeout=10)
            _docker_exec(container, "chmod +x /eval.sh", timeout=5)
            test_output, _ = _docker_exec(container, "/bin/bash /eval.sh", timeout=self.docker_timeout)
            log += f"\n[TEST OUTPUT]\n{test_output[-2000:]}\n"

            # Save logs
            with open(os.path.join(task_logs, "trace.log"), "w") as f:
                f.write(log)
            with open(os.path.join(task_logs, "test_output.txt"), "w") as f:
                f.write(test_output)

            # Parse reward
            reward = _parse_test_results(
                test_output, ctx["repo"],
                ctx.get("FAIL_TO_PASS", []), ctx.get("PASS_TO_PASS", []),
            )
            return VerifyResult(task.task_id, reward, log=log[-3000:])

        except Exception as e:
            return VerifyResult(task.task_id, 0.0, error=str(e)[:300], log=log[-3000:])
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)

    # ─── One-shot mode (legacy, for non-interactive routers) ───

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        work_dir = os.path.join(logs_dir or "/tmp", task.task_id.replace("/", "_"))
        os.makedirs(work_dir, exist_ok=True)
        pred_path = os.path.join(work_dir, "predictions.jsonl")
        run_id = f"single_{int(time.time())}"

        with open(pred_path, "w") as f:
            f.write(json.dumps({
                "instance_id": task.task_id,
                "model_name_or_path": "eval",
                "model_patch": answer,
            }) + "\n")

        cmd = [
            "conda", "run", "-n", self.conda_env,
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
            "conda", "run", "-n", self.conda_env,
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
