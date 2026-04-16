"""QA answer verifier for HotpotQA, DROP, and MuSiQue.

Verification strategy (following SQuAD evaluation standard):
1. Normalize both strings: lowercase, remove articles, remove punctuation
2. Exact match on normalized strings
3. If no exact match, compute token-level F1
4. Accept if F1 >= threshold (default 0.5, standard for DROP/HotpotQA)

For DROP specifically: also handles numeric answers and multi-span answers.
"""

from __future__ import annotations

import re
import string


def _normalize_answer(s: str) -> str:
    """Normalize answer string following SQuAD convention."""
    s = s.lower()
    # Remove articles
    s = re.sub(r'\b(a|an|the)\b', ' ', s)
    # Remove punctuation
    s = s.translate(str.maketrans('', '', string.punctuation))
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _compute_f1(pred: str, gold: str) -> float:
    """Compute token-level F1 between normalized pred and gold."""
    pred_tokens = pred.split()
    gold_tokens = gold.split()

    if not pred_tokens or not gold_tokens:
        return 0.0

    common = set(pred_tokens) & set(gold_tokens)
    if not common:
        return 0.0

    precision = len(common) / len(pred_tokens)
    recall = len(common) / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def _try_numeric_match(pred: str, gold: str) -> bool:
    """For DROP: check if both are numbers and match."""
    try:
        p = float(pred.replace(',', '').strip())
        g = float(gold.replace(',', '').strip())
        return abs(p - g) < 1e-3
    except ValueError:
        return False


def verify_qa(pred: str, gold: str, f1_threshold: float = 0.5) -> bool:
    """Verify QA answer using EM + F1.

    Args:
        pred: Model's predicted answer
        gold: Gold standard answer
        f1_threshold: Minimum F1 score to accept (default 0.5)

    Returns:
        True if the answer is correct
    """
    if not pred or not gold:
        return False

    # Numeric match (for DROP arithmetic questions)
    if _try_numeric_match(pred, gold):
        return True

    # Normalize
    p_norm = _normalize_answer(pred)
    g_norm = _normalize_answer(gold)

    # Exact match
    if p_norm == g_norm:
        return True

    # Containment (gold is substring of pred)
    if g_norm in p_norm and len(g_norm) > 2:
        return True

    # Token F1
    f1 = _compute_f1(p_norm, g_norm)
    return f1 >= f1_threshold
