"""
MRCR v2 (Multi-Round Context Reasoning) benchmark adapter.

Tests model ability to reason over information scattered across multiple
conversation rounds. Primarily a long-context + retrieval benchmark.

Dataset: MRCR v2 release
Format:  Multi-round conversation → final question requiring cross-round reasoning
Verify:  Exact match or F1 on extracted answer
"""
import re
import string
from typing import List
from .base import BaseBenchmark, Task, VerifyResult


def _normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'\b(a|an|the)\b', ' ', s)
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _f1(pred: str, gold: str) -> float:
    p_toks = _normalize(pred).split()
    g_toks = _normalize(gold).split()
    if not p_toks or not g_toks:
        return float(p_toks == g_toks)
    common = set(p_toks) & set(g_toks)
    if not common:
        return 0.0
    prec = len(common) / len(p_toks)
    rec = len(common) / len(g_toks)
    return 2 * prec * rec / (prec + rec)


def _extract_answer(text: str) -> str:
    m = re.search(r'(?:answer|result)\s*(?:is|:)\s*(.+?)(?:[.\n]|$)', text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'\\boxed\{([^}]+)\}', text)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    return lines[-1] if lines else text.strip()


class MRCR(BaseBenchmark):
    """MRCR v2: Multi-Round Context Reasoning benchmark."""
    scoring_mode = "uno_harness"
    score_name = "Uno harness score"

    def __init__(self, dataset="google/mrcr", split="test"):
        self.dataset = dataset
        self.split = split

    @property
    def name(self) -> str:
        return "MRCR-v2"

    def load(self, max_tasks=None) -> List[Task]:
        from datasets import load_dataset
        try:
            ds = load_dataset(self.dataset, split=self.split, trust_remote_code=True)
        except Exception:
            # Fallback: try loading from local or alternative source
            ds = load_dataset(self.dataset, split="validation", trust_remote_code=True)

        if max_tasks:
            ds = ds.select(range(min(max_tasks, len(ds))))

        tasks = []
        for i, row in enumerate(ds):
            # MRCR format: multi-round messages + final question
            messages = row.get("messages", row.get("conversations", []))
            question = row.get("question", "")
            gold = row.get("answer", row.get("expected_answer", ""))

            # If messages format, build context from conversation rounds
            if messages and not question:
                context_parts = []
                for msg in messages[:-1]:
                    role = msg.get("role", msg.get("from", ""))
                    content = msg.get("content", msg.get("value", ""))
                    context_parts.append(f"[{role}]: {content}")
                last_msg = messages[-1]
                question = last_msg.get("content", last_msg.get("value", ""))
                context_str = "\n\n".join(context_parts)
            else:
                context_str = row.get("context", "")

            prompt = ""
            if context_str:
                prompt += f"Conversation context:\n{context_str}\n\n"
            prompt += f"Question: {question}\n\nProvide a precise answer."

            tasks.append(Task(
                task_id=f"mrcr_{i}",
                raw=row,
                question=prompt,
                context={"messages": messages},
                gold=str(gold).strip(),
            ))
        return tasks

    def extract_answer(self, router_output: str, task: Task) -> str:
        return _extract_answer(router_output)

    def verify(self, task: Task, answer: str, logs_dir=None) -> VerifyResult:
        pred = _extract_answer(answer)
        gold = task.gold

        if _normalize(pred) == _normalize(gold):
            return VerifyResult(task.task_id, 1.0, log=f"exact pred={pred}")
        # Numeric
        try:
            if abs(float(pred.replace(',', '')) - float(gold.replace(',', ''))) < 1e-3:
                return VerifyResult(task.task_id, 1.0, log=f"numeric pred={pred}")
        except ValueError:
            pass
        # Containment
        if len(gold) > 2 and _normalize(gold) in _normalize(pred):
            return VerifyResult(task.task_id, 1.0, log=f"contains pred={pred}")

        f1 = _f1(pred, gold)
        return VerifyResult(
            task.task_id, 1.0 if f1 >= 0.8 else 0.0,
            log=f"f1={f1:.3f} pred={pred} gold={gold}",
        )
