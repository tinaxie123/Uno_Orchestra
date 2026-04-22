"""
GPQA Diamond benchmark adapter.

Dataset: Idavidrein/gpqa (gpqa_diamond subset)
Format:  Multiple-choice (A/B/C/D), graduate-level science QA
Verify:  Exact match on extracted choice letter
Size:    448 questions
"""
import re
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


def _extract_choice(text: str) -> str:
    """Extract choice letter (A-D) from model output."""
    text = text.strip()
    # Look for explicit "answer is (X)" patterns first
    m = re.search(r'(?:answer|choice)\s*(?:is|:)\s*\(?([A-Da-d])\)?', text, re.I)
    if m:
        return m.group(1).upper()
    # \\boxed{X}
    m = re.search(r'\\boxed\{([A-Da-d])\}', text)
    if m:
        return m.group(1).upper()
    # Last standalone letter (A)-(D) or A-D at end
    matches = re.findall(r'\(?([A-Da-d])\)?(?:\s*$|\s*[.。])', text)
    if matches:
        return matches[-1].upper()
    # First (A)/(B)/(C)/(D) that appears
    m = re.search(r'\(?([A-Da-d])\)', text)
    if m:
        return m.group(1).upper()
    return text.strip()[:1].upper()


class GPQA(BaseBenchmark):

    def __init__(self, subset="gpqa_diamond", split="train"):
        self.subset = subset
        self.split = split

    @property
    def name(self) -> str:
        return "GPQA"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("Idavidrein/gpqa", self.subset, split=self.split,
                          trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))
        tasks = []
        for i, row in enumerate(ds):
            # GPQA has: Question, Correct Answer, Incorrect Answer 1/2/3
            # Build multiple-choice format
            question = row.get("Question", row.get("question", ""))
            correct = row.get("Correct Answer", row.get("correct_answer", ""))
            choices = [correct]
            for k in range(1, 4):
                inc = row.get(f"Incorrect Answer {k}", row.get(f"incorrect_answer_{k}", ""))
                if inc:
                    choices.append(inc)

            # Shuffle deterministically based on index
            import hashlib
            seed = int(hashlib.md5(f"{i}_{question[:50]}".encode()).hexdigest(), 16)
            rng_indices = list(range(len(choices)))
            # Simple deterministic shuffle
            for j in range(len(rng_indices) - 1, 0, -1):
                k_idx = seed % (j + 1)
                rng_indices[j], rng_indices[k_idx] = rng_indices[k_idx], rng_indices[j]
                seed = seed // (j + 1) + seed % 7
            shuffled = [choices[idx] for idx in rng_indices]
            correct_idx = rng_indices.index(0)  # where the correct answer ended up
            gold_letter = chr(65 + correct_idx)  # A, B, C, D

            labels = "ABCD"
            formatted = question + "\n\nChoices:\n"
            for j, c in enumerate(shuffled):
                formatted += f"({labels[j]}) {c}\n"
            formatted += "\nAnswer with the letter of the correct choice (A, B, C, or D)."

            tasks.append(Task(
                task_id=f"gpqa_{i}",
                raw=row,
                question=formatted,
                context={"choices": shuffled, "correct_idx": correct_idx},
                gold=gold_letter,
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
