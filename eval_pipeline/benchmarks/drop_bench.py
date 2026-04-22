"""
DROP benchmark adapter.

Dataset: ucinlp/drop (test split)
Format:  Reading comprehension with discrete reasoning (counting, sorting, arithmetic)
Verify:  F1 score (token overlap) with threshold, following SQuAD-style evaluation
"""
import re
import string
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


def _normalize(s: str) -> str:
    """Normalize answer string for comparison."""
    s = s.lower()
    s = re.sub(r'\b(a|an|the)\b', ' ', s)
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _compute_f1(pred: str, gold: str) -> float:
    pred_toks = _normalize(pred).split()
    gold_toks = _normalize(gold).split()
    if not pred_toks or not gold_toks:
        return float(pred_toks == gold_toks)
    common = set(pred_toks) & set(gold_toks)
    if not common:
        return 0.0
    prec = len(common) / len(pred_toks)
    rec = len(common) / len(gold_toks)
    return 2 * prec * rec / (prec + rec)


def _extract_answer(text: str) -> str:
    """Extract answer from model output."""
    # "the answer is X"
    m = re.search(r'(?:answer|result)\s*(?:is|:)\s*(.+?)(?:[.\n]|$)', text, re.I)
    if m:
        return m.group(1).strip()
    # \\boxed{X}
    m = re.search(r'\\boxed\{([^}]+)\}', text)
    if m:
        return m.group(1).strip()
    # Last line / last sentence
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    return lines[-1] if lines else text.strip()


class DROP(BaseBenchmark):

    def __init__(self, split="validation"):
        # DROP test set has no public labels; use validation
        self.split = split

    @property
    def name(self) -> str:
        return "DROP"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        ds = load_dataset("ucinlp/drop", split=self.split, trust_remote_code=True)
        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for i, row in enumerate(ds):
            passage = row.get("passage", "")
            question = row.get("question", "")

            # Gold answers: spans_text + number + date
            golds = []
            ans = row.get("answers_spans", {})
            if isinstance(ans, dict):
                spans = ans.get("spans", [])
                if isinstance(spans, list):
                    golds.extend(spans)
                types = ans.get("types", [])
            # Also check number/date fields
            if not golds:
                num = row.get("answer_number", "")
                if num and str(num).strip():
                    golds.append(str(num).strip())

            gold_str = golds[0] if golds else ""

            prompt = (
                f"Read the passage and answer the question.\n\n"
                f"Passage: {passage}\n\n"
                f"Question: {question}\n\n"
                f"Give a short, precise answer."
            )

            tasks.append(Task(
                task_id=f"drop_{i}",
                raw=row,
                question=prompt,
                context={"all_golds": golds},
                gold=gold_str,
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_answer(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_answer(answer)
        all_golds = task.context.get("all_golds", [task.gold]) if task.context else [task.gold]
        if not all_golds:
            all_golds = [task.gold]

        # Exact match or numeric match against any gold
        for g in all_golds:
            if _normalize(pred) == _normalize(g):
                return VerifyResult(task.task_id, 1.0, log=f"exact pred={pred} gold={g}")
            # Numeric match
            try:
                if abs(float(pred.replace(',', '')) - float(g.replace(',', ''))) < 1e-3:
                    return VerifyResult(task.task_id, 1.0, log=f"numeric pred={pred} gold={g}")
            except ValueError:
                pass

        # F1 against best gold
        best_f1 = max(_compute_f1(pred, g) for g in all_golds)
        correct = best_f1 >= 0.8  # strict threshold
        return VerifyResult(
            task.task_id, 1.0 if correct else 0.0,
            log=f"f1={best_f1:.3f} pred={pred} gold={all_golds[0]}",
        )
