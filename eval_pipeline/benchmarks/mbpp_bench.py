"""
MBPP benchmark adapter.

Dataset: google-research-datasets/mbpp (sanitized subset)
Format:  Python function generation from natural language description
Verify:  Execute code + run assert-based test cases
Size:    500 (sanitized test split: 257)
"""
import re
import os
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
    # If it looks like code directly
    if re.search(r'\bdef\b', text):
        lines = []
        in_code = False
        for line in text.split('\n'):
            if re.match(r'(def |class |import |from |    )', line) or in_code:
                lines.append(line)
                in_code = True
            elif in_code and line.strip() == '':
                lines.append(line)
            elif in_code:
                break
        return '\n'.join(lines)
    return text.strip()


def _run_tests(code: str, tests: list[str]) -> tuple[bool, str]:
    """Run test assertions against code."""
    test_block = "\n".join(tests)
    full = code + "\n\n" + test_block + "\n"

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(full)
        tmp = f.name

    try:
        result = subprocess.run(
            ["python3", tmp],
            capture_output=True, text=True, timeout=EXEC_TIMEOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        passed = result.returncode == 0
        log = result.stdout[-300:] + result.stderr[-300:]
        return passed, log
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:300]
    finally:
        os.unlink(tmp)


class MBPP(BaseBenchmark):

    def __init__(self, subset="sanitized", split="test"):
        self.subset = subset
        self.split = split

    @property
    def name(self) -> str:
        return "MBPP"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("google-research-datasets/mbpp", self.subset,
                          split=self.split, trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for row in ds:
            task_id = str(row.get("task_id", row.get("index", 0)))
            text = row["text"]  # natural language description
            test_list = row.get("test_list", [])
            code = row.get("code", "")

            # Show one test as example
            example = f"\n\nExample test:\n{test_list[0]}" if test_list else ""
            question = (
                f"Write a Python function to solve the following problem.\n\n"
                f"{text}{example}\n\n"
                f"Provide only the function implementation in a Python code block."
            )

            tasks.append(Task(
                task_id=f"mbpp_{task_id}",
                raw=row,
                question=question,
                context={"test_list": test_list},
                gold=code,
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_code(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        code = _extract_code(answer)
        tests = task.context.get("test_list", [])
        if not tests:
            return VerifyResult(task.task_id, 0.0, error="no tests")
        passed, log = _run_tests(code, tests)
        return VerifyResult(
            task.task_id, 1.0 if passed else 0.0,
            log=log[:500],
        )
