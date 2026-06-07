from __future__ import annotations

import re
import string


def _normalize_answer(s: str) -> str:
    
    s = s.lower()
    s = re.sub(r'\b(a|an|the)\b', ' ', s)
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _compute_f1(pred: str, gold: str) -> float:
    
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
  
    try:
        p = float(pred.replace(',', '').strip())
        g = float(gold.replace(',', '').strip())
        return abs(p - g) < 1e-3
    except ValueError:
        return False


def verify_qa(pred: str, gold: str, f1_threshold: float = 0.5) -> bool:
    
    if not pred or not gold:
        return False

    if _try_numeric_match(pred, gold):
        return True

    p_norm = _normalize_answer(pred)
    g_norm = _normalize_answer(gold)

    if p_norm == g_norm:
        return True

    if g_norm in p_norm and len(g_norm) > 2:
        return True

    f1 = _compute_f1(p_norm, g_norm)
    return f1 >= f1_threshold
