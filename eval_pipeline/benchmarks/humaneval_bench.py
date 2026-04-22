"""
HumanEval benchmark adapter.

Dataset: openai/openai_humaneval
Format:  Python function completion
Verify:  Execute code + run test cases
Size:    164 problems
"""
import re
import os
import subprocess
import tempfile
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


EXEC_TIMEOUT = 15  # seconds per test


def _extract_code(text: str, prompt: str = "") -> str:
    """Extract Python code from model output."""
    # ```python ... ```
    m = re.search(r'```python\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # ``` ... ```
    m = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # If output starts with def/class or indented code, take it directly
    if re.match(r'\s*(def |class |    )', text):
        return text.strip()
    # If prompt given, try prompt + output as completion
    if prompt and not text.strip().startswith('def '):
        return prompt + text
    return text.strip()


def _run_code(code: str, test_code: str, entry_point: str) -> tuple[bool, str]:
    """Execute code + tests in subprocess. Returns (passed, log)."""
    full_code = code + "\n\n" + test_code + f"\n\ncheck({entry_point})\n"

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(full_code)
        tmp = f.name

    try:
        result = subprocess.run(
            ["python3", tmp],
            capture_output=True, text=True, timeout=EXEC_TIMEOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        passed = result.returncode == 0
        log = result.stdout[-500:] + result.stderr[-500:]
        return passed, log
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:300]
    finally:
        os.unlink(tmp)


class HumanEval(BaseBenchmark):

    def __init__(self):
        pass

    @property
    def name(self) -> str:
        return "HumanEval"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("openai/openai_humaneval", split="test",
                          trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for row in ds:
            task_id = row["task_id"]  # e.g., "HumanEval/0"
            prompt = row["prompt"]  # function signature + docstring
            test = row["test"]  # check() function
            entry = row["entry_point"]
            canonical = row.get("canonical_solution", "")

            question = (
                f"Complete the following Python function.\n\n"
                f"{prompt}\n\n"
                f"Provide only the complete function implementation in a Python code block."
            )

            tasks.append(Task(
                task_id=task_id,
                raw=row,
                question=question,
                context={"prompt": prompt, "test": test, "entry_point": entry},
                gold=canonical,
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_code(router_output, task.context.get("prompt", ""))

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        code = _extract_code(answer, task.context.get("prompt", ""))
        # Ensure the function prompt is included
        prompt = task.context["prompt"]
        if prompt.strip().split('\n')[0] not in code:
            code = prompt + code

        test = task.context["test"]
        entry = task.context["entry_point"]
        passed, log = _run_code(code, test, entry)
        return VerifyResult(
            task.task_id, 1.0 if passed else 0.0,
            log=log[:500],
        )
