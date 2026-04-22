"""
AIME 2025 benchmark adapter.

Dataset: AI-MO/aimo-validation-aime (or Maxwell-Jia/AIME_2025)
Format:  Competition math, integer answer 0-999
Verify:  Exact integer match
"""
import re
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


def _extract_integer(text: str) -> str:
    """Extract integer answer from model output."""
    # \\boxed{N}
    m = re.search(r'\\boxed\{(\d+)\}', text)
    if m:
        return m.group(1)
    # "answer is N"
    m = re.search(r'(?:answer|result)\s*(?:is|=|:)\s*(\d+)', text, re.I)
    if m:
        return m.group(1)
    # Last integer in text
    nums = re.findall(r'\b(\d{1,3})\b', text)
    if nums:
        return nums[-1]
    return text.strip()


class AIME(BaseBenchmark):
    """AIME 2025 — American Invitational Mathematics Examination."""

    def __init__(self, dataset="Maxwell-Jia/AIME_2025", split="train"):
        self.dataset = dataset
        self.split = split

    @property
    def name(self) -> str:
        return "AIME-2025"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        try:
            ds = load_dataset(self.dataset, split=self.split, trust_remote_code=True)
        except Exception:
            # Fallback: try AI-MO dataset
            ds = load_dataset("AI-MO/aimo-validation-aime", split="train",
                              trust_remote_code=True)

        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for i, row in enumerate(ds):
            question = row.get("problem", row.get("question", ""))
            gold = str(row.get("answer", row.get("expected_answer", "")))

            prompt = (
                question + "\n\n"
                "This is an AIME problem. The answer is an integer between 0 and 999 inclusive. "
                "Put your final answer in \\boxed{}."
            )

            tasks.append(Task(
                task_id=f"aime_{i}",
                raw=row,
                question=prompt,
                context={},
                gold=gold.strip(),
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_integer(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_integer(answer)
        try:
            correct = int(pred) == int(task.gold)
        except ValueError:
            correct = pred.strip() == task.gold.strip()
        return VerifyResult(
            task.task_id, 1.0 if correct else 0.0,
            log=f"pred={pred} gold={task.gold}",
        )
