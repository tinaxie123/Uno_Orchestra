"""
LiveCodeBench v6 benchmark adapter.

Dataset: livecodebench/code_generation_lite (v6, after 2025-01)
Format:  Code generation from problem descriptions, stdin/stdout
Verify:  Execute code and compare output against test cases
"""
import re
import os
import json
import subprocess
import tempfile
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


EXEC_TIMEOUT = 15


def _extract_code(text: str) -> str:
    """Extract Python code from model output."""
    m = re.search(r'```python\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _run_with_io(code: str, stdin: str, expected: str) -> tuple[bool, str]:
    """Run code with stdin, compare stdout to expected output."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        tmp = f.name

    try:
        result = subprocess.run(
            ["python3", tmp],
            input=stdin, capture_output=True, text=True,
            timeout=EXEC_TIMEOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        actual = result.stdout.strip()
        expected_clean = expected.strip()
        passed = actual == expected_clean
        log = f"exit={result.returncode} actual={actual[:200]} expected={expected_clean[:200]}"
        if result.stderr:
            log += f" stderr={result.stderr[:200]}"
        return passed, log
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:300]
    finally:
        os.unlink(tmp)


class LiveCodeBench(BaseBenchmark):
    """LiveCodeBench v6 — live competitive programming problems."""

    def __init__(self, version="v6", split="test"):
        self.version = version
        self.split = split

    @property
    def name(self) -> str:
        return f"LiveCodeBench-{self.version}"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        # LiveCodeBench lite version with test cases included
        try:
            ds = load_dataset("livecodebench/code_generation_lite",
                              split=self.split, trust_remote_code=True)
        except Exception:
            ds = load_dataset("livecodebench/code_generation",
                              split=self.split, trust_remote_code=True)

        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for i, row in enumerate(ds):
            qid = row.get("question_id", str(i))
            title = row.get("question_title", "")
            content = row.get("question_content", row.get("question", ""))
            difficulty = row.get("difficulty", "")

            # Parse test cases
            public_tests = row.get("public_test_cases", "[]")
            if isinstance(public_tests, str):
                try:
                    public_tests = json.loads(public_tests)
                except json.JSONDecodeError:
                    public_tests = []

            private_tests = row.get("private_test_cases", "[]")
            if isinstance(private_tests, str):
                try:
                    private_tests = json.loads(private_tests)
                except json.JSONDecodeError:
                    private_tests = []

            all_tests = public_tests + private_tests

            prompt = (
                f"{content}\n\n"
                f"Write a Python solution that reads from stdin and writes to stdout. "
                f"Provide the complete code in a Python code block."
            )

            tasks.append(Task(
                task_id=f"lcb_{qid}",
                raw=row,
                question=prompt,
                context={
                    "title": title,
                    "difficulty": difficulty,
                    "test_cases": all_tests,
                },
                gold="",
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_code(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        code = _extract_code(answer)
        tests = task.context.get("test_cases", [])

        if not tests:
            return VerifyResult(task.task_id, 0.0, error="no test cases")

        passed_count = 0
        total = len(tests)
        logs = []

        for j, tc in enumerate(tests):
            if isinstance(tc, dict):
                inp = tc.get("input", "")
                exp = tc.get("output", tc.get("expected_output", ""))
            elif isinstance(tc, (list, tuple)) and len(tc) >= 2:
                inp, exp = tc[0], tc[1]
            else:
                continue

            ok, log = _run_with_io(code, str(inp), str(exp))
            if ok:
                passed_count += 1
            logs.append(f"tc{j}: {'PASS' if ok else 'FAIL'}")

            # Early termination on first failure for efficiency
            if not ok:
                break

        all_passed = passed_count == total
        return VerifyResult(
            task.task_id,
            1.0 if all_passed else 0.0,
            log=f"{passed_count}/{total} passed. " + "; ".join(logs[:5]),
        )
