"""ToolACE answer verifier.

Verification strategy:
1. Exact match on normalized text
2. Function/API call name overlap: extract all function call patterns from
   both pred and gold, check if overlap ratio >= threshold
3. Argument-level matching: if function names match, check key arguments

ToolACE gold answers are assistant responses containing function calls,
so we compare the structure of the tool usage rather than exact strings.
"""

from __future__ import annotations

import json
import re


def _normalize_text(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def _extract_function_calls(text: str) -> list[dict]:
    """Extract function call patterns from text.

    Handles formats:
    - function_name(arg1=val1, arg2=val2)
    - {"name": "func", "arguments": {...}}
    """
    calls = []

    # JSON format (common in ToolACE)
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}', text):
        try:
            # Try to parse the full JSON object
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

    # Python-style function calls
    if not calls:
        for m in re.finditer(r'(\w+(?:\.\w+)*)\(([^)]*)\)', text):
            calls.append({"name": m.group(1), "arguments": m.group(2)})

    return calls


def verify_toolace(pred: str, gold: str, name_overlap_threshold: float = 0.5) -> bool:
    """Verify ToolACE answer by comparing tool call structure.

    Args:
        pred: Model's predicted response
        gold: Gold standard response
        name_overlap_threshold: Minimum ratio of matching function names

    Returns:
        True if the tool usage structure matches
    """
    if not pred or not gold:
        return False

    # Exact text match
    if _normalize_text(pred) == _normalize_text(gold):
        return True

    # Extract and compare function calls
    pred_calls = _extract_function_calls(pred)
    gold_calls = _extract_function_calls(gold)

    if not gold_calls:
        # Gold has no function calls: fall back to text containment
        g = _normalize_text(gold)
        p = _normalize_text(pred)
        return g in p if len(g) > 2 else p == g

    if not pred_calls:
        return False

    # Compare function names
    pred_names = {c["name"].lower() for c in pred_calls}
    gold_names = {c["name"].lower() for c in gold_calls}

    if not gold_names:
        return False

    overlap = len(pred_names & gold_names) / len(gold_names)
    return overlap >= name_overlap_threshold
