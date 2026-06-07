"""
GAIA benchmark adapter.

Dataset: gaia-benchmark/GAIA (2023 version)
Format:  Long-horizon multi-tool reasoning, short answer
Verify:  Exact match (normalized) or numeric match
Size:    ~165 (validation, test has no labels)
"""
import re
import string
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


def _normalize(s: str) -> str:
    """Normalize answer for comparison."""
    s = s.lower().strip()
    s = re.sub(r'\b(a|an|the)\b', ' ', s)
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _extract_answer(text: str) -> str:
    """Extract final answer from model output."""
    # FINAL ANSWER: X
    m = re.search(r'(?:FINAL\s+ANSWER|final\s+answer)\s*[:：]\s*(.+?)(?:\n|$)', text, re.I)
    if m:
        return m.group(1).strip()
    # "the answer is X"
    m = re.search(r'(?:answer|result)\s*(?:is|:)\s*(.+?)(?:[.\n]|$)', text, re.I)
    if m:
        return m.group(1).strip()
    # \\boxed{X}
    m = re.search(r'\\boxed\{([^}]+)\}', text)
    if m:
        return m.group(1).strip()
    # Last non-empty line
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    return lines[-1] if lines else text.strip()


class GAIA(BaseBenchmark):
    """GAIA: General AI Assistants benchmark."""
    scoring_mode = "uno_harness"
    score_name = "Uno harness score"

    def __init__(self, split="validation"):
        # test split has no labels; use validation
        self.split = split

    @property
    def name(self) -> str:
        return "GAIA"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("gaia-benchmark/GAIA", "2023_all",
                          split=self.split, trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for i, row in enumerate(ds):
            question = row.get("Question", row.get("question", ""))
            gold = row.get("Final answer", row.get("final_answer", ""))
            level = row.get("Level", row.get("level", ""))
            steps = row.get("Annotator Metadata", {})

            # GAIA instructs models to give short final answers
            prompt = (
                f"{question}\n\n"
                f"Provide a short, precise final answer. "
                f"Format: FINAL ANSWER: <your answer>"
            )

            tasks.append(Task(
                task_id=f"gaia_{i}",
                raw=row,
                question=prompt,
                context={"level": level, "metadata": steps},
                gold=str(gold).strip(),
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_answer(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_answer(answer)
        gold = task.gold

        # Exact normalized match
        if _normalize(pred) == _normalize(gold):
            return VerifyResult(task.task_id, 1.0, log=f"exact pred={pred} gold={gold}")

        # Numeric match
        try:
            p = float(pred.replace(',', '').strip())
            g = float(gold.replace(',', '').strip())
            if abs(p - g) < 1e-3:
                return VerifyResult(task.task_id, 1.0, log=f"numeric pred={p} gold={g}")
        except ValueError:
            pass

        # Substring containment (gold is usually short)
        if len(gold) > 2 and _normalize(gold) in _normalize(pred):
            return VerifyResult(task.task_id, 1.0, log=f"contains pred={pred} gold={gold}")

        return VerifyResult(
            task.task_id, 0.0,
            log=f"pred={pred} gold={gold}",
        )
