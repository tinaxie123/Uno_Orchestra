"""
MMLU benchmark adapter.

Dataset: cais/mmlu (all subjects)
Format:  Multiple-choice (A/B/C/D), multi-domain knowledge
Verify:  Exact match on choice letter
Size:    14,042 test questions (57 subjects)
"""
import re
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


LABELS = "ABCD"


def _extract_choice(text: str) -> str:
    """Extract choice letter from model output."""
    text = text.strip()
    m = re.search(r'(?:answer|choice)\s*(?:is|:)\s*\(?([A-Da-d])\)?', text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r'\\boxed\{([A-Da-d])\}', text)
    if m:
        return m.group(1).upper()
    matches = re.findall(r'\(?([A-Da-d])\)?(?:\s*$|\s*[.。])', text)
    if matches:
        return matches[-1].upper()
    m = re.search(r'\(?([A-Da-d])\)', text)
    if m:
        return m.group(1).upper()
    return text.strip()[:1].upper()


class MMLU(BaseBenchmark):

    def __init__(self, subset="all", split="test"):
        self.subset = subset
        self.split = split

    @property
    def name(self) -> str:
        return "MMLU"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("cais/mmlu", self.subset, split=self.split,
                          trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))
        tasks = []
        for i, row in enumerate(ds):
            question = row["question"]
            choices = row["choices"]
            gold_idx = row["answer"]  # integer 0-3
            subject = row.get("subject", "unknown")

            formatted = f"[{subject}] {question}\n\n"
            for j, c in enumerate(choices):
                formatted += f"({LABELS[j]}) {c}\n"
            formatted += "\nAnswer with the letter of the correct choice (A, B, C, or D)."

            tasks.append(Task(
                task_id=f"mmlu_{i}",
                raw=row,
                question=formatted,
                context={"subject": subject},
                gold=LABELS[gold_idx],
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_choice(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_choice(answer)
        correct = pred == task.gold
        return VerifyResult(
            task.task_id, 1.0 if correct else 0.0,
            log=f"pred={pred} gold={task.gold}",
        )
