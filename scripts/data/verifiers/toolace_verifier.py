from __future__ import annotations
import json
import re

def _normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'\s+', ' ', s)
    return s

def _extract_function_calls(text: str) -> list[dict]:
    calls = []
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}', text):
        try:          
            start = m.start()
            brace_count = 0
            end = start
            for i in range(start, len(text)):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                if brace_count == 0:
                    end = i + 1
                    break
            obj = json.loads(text[start:end])
            calls.append({
                "name": obj.get("name", ""),
                "arguments": obj.get("arguments", {}),
            })
        except (json.JSONDecodeError, IndexError):
            calls.append({"name": m.group(1), "arguments": {}})
    if not calls:
        for m in re.finditer(r'(\w+(?:\.\w+)*)\(([^)]*)\)', text):
            calls.append({"name": m.group(1), "arguments": m.group(2)})

    return calls


def verify_toolace(pred: str, gold: str, name_overlap_threshold: float = 0.5) -> bool:

    if not pred or not gold:
        return False

    if _normalize_text(pred) == _normalize_text(gold):
        return True

    pred_calls = _extract_function_calls(pred)
    gold_calls = _extract_function_calls(gold)

    if not gold_calls:
        # Gold is not a tool call (e.g., a refusal or text answer)
        # Use text similarity: substring match or F1-like overlap
        g = _normalize_text(gold)
        p = _normalize_text(pred)
        if g in p if len(g) > 2 else p == g:
            return True
        # Also check if both are refusals (common in toolace)
        refusal_patterns = ["don't have", "cannot", "can't", "not available",
                           "unable to", "no access", "not capable"]
        gold_is_refusal = any(pat in g for pat in refusal_patterns)
        pred_is_refusal = any(pat in p for pat in refusal_patterns)
        if gold_is_refusal and pred_is_refusal:
            return True
        return False

    if not pred_calls:
        return False

    pred_names = {c["name"].lower() for c in pred_calls}
    gold_names = {c["name"].lower() for c in gold_calls}

    if not gold_names:
        return False

    overlap = len(pred_names & gold_names) / len(gold_names)
    return overlap >= name_overlap_threshold
