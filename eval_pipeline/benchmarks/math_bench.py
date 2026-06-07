"""
MATH-500 benchmark adapter.

Dataset: HuggingFaceH4/MATH-500 (curated 500-problem subset of MATH)
Format:  Free-form math, answer in \\boxed{}
Verify:  Math equivalence (numeric + LaTeX normalization)
Size:    500 problems
"""
import re
import sys
import os
from typing import List
from .base import BaseBenchmark, Task, VerifyResult

# Reuse the Uno verifier used by the RL environment.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from env.env_package.uno.verifiers.math_verifier import verify_math


def _extract_boxed(text: str) -> str:
    """Extract answer from \\boxed{...}, falling back to last number."""
    # Find all \\boxed{...} (handling nested braces)
    matches = []
    for m in re.finditer(r'\\boxed\{', text):
        start = m.end()
        depth = 1
        pos = start
        while pos < len(text) and depth > 0:
            if text[pos] == '{':
                depth += 1
            elif text[pos] == '}':
                depth -= 1
            pos += 1
        if depth == 0:
            matches.append(text[start:pos - 1])
    if matches:
        return matches[-1].strip()

    # Fallback: "the answer is X"
    m = re.search(r'(?:answer|result)\s*(?:is|=|:)\s*(.+?)(?:[.\n]|$)', text, re.I)
    if m:
        return m.group(1).strip()

    return text.strip()


class MATH500(BaseBenchmark):

    def __init__(self, dataset="HuggingFaceH4/MATH-500", split="test"):
        self.dataset = dataset
        self.split = split

    @property
    def name(self) -> str:
        return "MATH-500"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset(self.dataset, split=self.split, trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))
        tasks = []
        for i, row in enumerate(ds):
            question = row.get("problem", row.get("question", ""))
            # Gold answer: extract from solution's \\boxed{}
            solution = row.get("solution", row.get("answer", ""))
            gold = _extract_boxed(solution) if "\\boxed" in solution else solution.strip()
            level = row.get("level", "")
            subject = row.get("type", row.get("subject", ""))

            prompt = question + "\n\nPut your final answer in \\boxed{}."

            tasks.append(Task(
                task_id=f"math_{i}",
                raw=row,
                question=prompt,
                context={"level": level, "subject": subject, "solution": solution},
                gold=gold,
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_boxed(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_boxed(answer)
        correct = verify_math(pred, task.gold)
        return VerifyResult(
            task.task_id, 1.0 if correct else 0.0,
            log=f"pred={pred} gold={task.gold}",
        )
