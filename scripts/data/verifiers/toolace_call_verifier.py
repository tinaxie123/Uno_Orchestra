"""
ToolACE function-call verifier aligned with BFCL AST matching standard.

BFCL protocol (Berkeley Function-Calling Leaderboard):
  - Parse predicted function call into AST
  - Function name: exact match (case-insensitive)
  - Parameters: all required param names + values + types must match
  - Scoring: BINARY (1.0 or 0.0), no partial credit
  - Supports simple, multiple, parallel, parallel_multiple

Beyond the JSON / Python-call formats BFCL canonicalises, ToolACE's own
training set stores gold answers in two non-standard wrappers:

  - bracket-dash: `[Fn-["k1"-v1;"k2"-v2], Fn2-["k"-v]]`
  - angle-pipe:   `<Fn|<k1|v1,k2|v2>, Fn2|<k|v>>`

These are the formats the prompt instructs the model to emit (see
data/rl/train.parquet toolace rows), so gold and prediction use the
same shape. We parse them here into {name, arguments} dicts so the
same AST matcher can score them. Value-level fidelity is weaker than
JSON (values can contain unescaped commas/dashes/pipes) — see
`_parse_toolace_*` for the trade-offs.

For RL reward, we use:
  - strict mode (default): binary, aligned with BFCL
  - lenient mode: partial credit for name-only match (for early RL warm-up)
"""
from __future__ import annotations

import json
import re
from typing import Any


def _parse_func_calls(text: str) -> list[dict[str, Any]] | None:
    """Parse function call(s) from various formats.

    Returns list of {"name": str, "arguments": dict} or None.
    """
    text = text.strip()

    # Strip outer brackets [...]
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1].strip()

    calls = []

    # Format 1: JSON — try full parse first
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("arguments", obj.get("params", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            return [{"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}]
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and "name" in item:
                    args = item.get("arguments", item.get("params", {}))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    calls.append({"name": item["name"], "arguments": args if isinstance(args, dict) else {}})
            if calls:
                return calls
    except (json.JSONDecodeError, TypeError):
        pass

    # Format 1b: Find JSON objects with "name" in free text (handles nested braces)
    for m in re.finditer(r'\{"name"', text):
        start = m.start()
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            if depth == 0:
                end = i + 1
                break
        try:
            obj = json.loads(text[start:end])
            if "name" in obj:
                args = obj.get("arguments", obj.get("params", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                calls.append({"name": obj["name"], "arguments": args if isinstance(args, dict) else {}})
        except json.JSONDecodeError:
            pass

    if calls:
        return calls

    # Format 2: FuncName(key="val", key2=val2) — Python-like
    for m in re.finditer(r'([\w.]+)\(([^)]*)\)', text):
        name = m.group(1)
        args_str = m.group(2).strip()
        args = _parse_kwargs(args_str)
        calls.append({"name": name, "arguments": args})

    if calls:
        return calls

    # Format 3: ToolACE bracket-dash  `[Fn-["k"-v;"k"-v], Fn2-[...]]`
    tb = _parse_toolace_bracket_dash(text)
    if tb:
        return tb

    # Format 4: ToolACE angle-pipe    `<Fn|<k|v,k|v>, Fn2|<...>>`
    ta = _parse_toolace_angle_pipe(text)
    if ta:
        return ta

    return None


# ── ToolACE native-format parsers ───────────────────────────────
#
# These formats encode argument values without a uniform quoting rule
# (lists, scalars, and quoted strings all coexist). We recover function
# names and the SET of argument keys robustly; argument values are kept
# as raw strings and normalised downstream. This means value-level
# equality under these formats is weaker than for JSON gold — good
# enough for the lenient RL reward, not a substitute for a proper BFCL
# AST at eval time.

_TOOLACE_BRACKET_FN_RE = re.compile(r'([A-Za-z_][\w\-]*)\s*-\s*\[')
_TOOLACE_BRACKET_KV_RE = re.compile(r'"([^"]+)"\s*-\s*([^;\]]+?)(?=\s*;|\s*\])')
_TOOLACE_ANGLE_FN_RE   = re.compile(r'([A-Za-z_][\w\- ]*?)\s*\|\s*<')
_TOOLACE_ANGLE_KV_RE   = re.compile(r"([A-Za-z_][\w\- ]*?)\s*\|\s*('(?:[^']*)'|\"(?:[^\"]*)\"|[^,<>]+)")


def _parse_toolace_bracket_dash(text: str) -> list[dict[str, Any]] | None:
    """Parse ToolACE's `[Fn-["k"-v;"k"-v], ...]` wire format.

    We locate each `Fn-[` anchor, then scan forward with bracket-depth
    tracking to find the matching `]`. Inside the args block, keys
    appear as `"key"-value` separated by `;` (with trailing `]`).
    """
    anchors = list(_TOOLACE_BRACKET_FN_RE.finditer(text))
    if not anchors:
        return None
    calls: list[dict[str, Any]] = []
    for m in anchors:
        name = m.group(1)
        # Walk from the opening `[` to its match.
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == '[':
                depth += 1
            elif text[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = text[start + 1:end]
        args: dict[str, Any] = {}
        for km in _TOOLACE_BRACKET_KV_RE.finditer(body):
            args[km.group(1)] = km.group(2).strip()
        calls.append({"name": name, "arguments": args})
    return calls if calls else None


def _parse_toolace_angle_pipe(text: str) -> list[dict[str, Any]] | None:
    """Parse ToolACE's `<Fn|<k|v,k|v>, ...>` wire format."""
    anchors = list(_TOOLACE_ANGLE_FN_RE.finditer(text))
    if not anchors:
        return None
    calls: list[dict[str, Any]] = []
    for m in anchors:
        name = m.group(1).strip()
        # Walk from the opening `<` of the arg block to its match.
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == '<':
                depth += 1
            elif text[i] == '>':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = text[start + 1:end]
        args: dict[str, Any] = {}
        for km in _TOOLACE_ANGLE_KV_RE.finditer(body):
            args[km.group(1).strip()] = km.group(2).strip().strip("'\"")
        calls.append({"name": name, "arguments": args})
    return calls if calls else None


def _parse_kwargs(args_str: str) -> dict[str, Any]:
    """Parse keyword arguments from 'key="val", key2=val2'."""
    args: dict[str, Any] = {}
    # Match key="value" or key='value' or key=number or key=True/False
    for m in re.finditer(
        r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([\w.+-]+))',
        args_str,
    ):
        key = m.group(1)
        val: Any = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        # Type coercion
        if val is not None:
            if val.lower() == 'true':
                val = True
            elif val.lower() == 'false':
                val = False
            else:
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass  # Keep as string
        args[key] = val
    return args


_REFUSAL_PATTERNS = (
    "don't have", "do not have", "cannot", "can't", "not available",
    "unable to", "no access", "not capable", "not provided",
    "please provide", "could you provide", "need more", "insufficient",
)


def _normalize_value(v: Any) -> str:
    """Normalize a parameter value for comparison."""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).strip().strip('"').strip("'")
    return s


def _calls_match(pred_call: dict, gold_call: dict) -> bool:
    """Check if a single predicted call matches the gold call (BFCL strict).

    Requirements:
      1. Function name exact match (case-insensitive, ignore underscores/hyphens)
      2. All gold argument keys present in pred
      3. All argument values match (normalized string comparison)
    """
    # Defensive: some parser paths (free-text JSON recovery) can yield
    # a dict/None in `name` when the source is malformed. Treat those
    # as non-matching rather than crashing the reward loop mid-batch.
    pn, gn = pred_call.get("name"), gold_call.get("name")
    if not isinstance(pn, str) or not isinstance(gn, str):
        return False
    # Function name match
    pred_name = pn.lower().replace("_", "").replace("-", "").replace(".", "")
    gold_name = gn.lower().replace("_", "").replace("-", "").replace(".", "")
    if pred_name != gold_name:
        return False

    # Argument match
    gold_args = gold_call.get("arguments", {})
    pred_args = pred_call.get("arguments", {})

    if not gold_args:
        return True  # No args to check

    for key, gold_val in gold_args.items():
        # Case-insensitive key lookup
        pred_val = None
        for pk, pv in pred_args.items():
            if pk.lower() == key.lower():
                pred_val = pv
                break

        if pred_val is None:
            return False  # Missing required argument

        if _normalize_value(pred_val) != _normalize_value(gold_val):
            return False  # Value mismatch

    return True


def verify_toolace(pred: str, gold: str, strict: bool = True) -> float:
    """Verify ToolACE API call prediction.

    Args:
        pred: Model's prediction
        gold: Gold answer
        strict: If True, binary (BFCL standard). If False, partial credit.

    Returns:
        strict mode:  1.0 (all calls match) or 0.0 (any mismatch)
        lenient mode: fraction of matched calls
    """
    if not pred or not gold:
        return 0.0

    gold_calls = _parse_func_calls(gold)
    pred_calls = _parse_func_calls(pred)

    if not gold_calls:
        # Gold is natural-language (refusal / clarification request).
        # Strict string equality is far too harsh for free-text — 76% of
        # our ToolACE training pool lands here. Fall through a ladder:
        #   1) exact normalised match
        #   2) gold is a short substring of pred (covers "Please provide
        #      your X" style clarifications restated verbatim by the model)
        #   3) both sides are refusals (shared intent even if wording
        #      diverges) — routed through `_REFUSAL_PATTERNS`.
        g = _normalize_value(gold).lower()
        p = _normalize_value(pred).lower()
        if not p:
            return 0.0
        if g == p:
            return 1.0
        if len(g) > 4 and g in p:
            return 1.0
        g_ref = any(pat in g for pat in _REFUSAL_PATTERNS)
        p_ref = any(pat in p for pat in _REFUSAL_PATTERNS)
        if g_ref and p_ref:
            return 1.0
        return 0.0

    if not pred_calls:
        return 0.0

    # Match: each gold call must have a matching pred call
    matched = 0
    used_pred = set()

    for gold_call in gold_calls:
        for j, pred_call in enumerate(pred_calls):
            if j in used_pred:
                continue
            if _calls_match(pred_call, gold_call):
                matched += 1
                used_pred.add(j)
                break

    if strict:
        # BFCL: all gold calls must match, no extra pred calls penalty
        return 1.0 if matched == len(gold_calls) else 0.0
    else:
        # Lenient: fraction of gold calls matched
        return matched / len(gold_calls)
