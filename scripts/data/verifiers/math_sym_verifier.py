"""
Symbolic math verifier for NuminaMath tasks.

Handles multiple gold answer formats:
  - Numeric: "42", "3.14", "-7/3"
  - LaTeX: "\\frac{1}{10}", "\\sqrt{2}", "2^{10}"
  - Multiple choice: "A", "B", "C"
  - Expressions: "n = 11", "x + 2y"

Verification cascade:
  1. Exact string match (after normalization)
  2. Numeric equivalence (float comparison with tolerance)
  3. Symbolic equivalence (sympy parse + simplify)

Returns float in [0, 1].
"""
from __future__ import annotations

import re
import math


def _clean(s: str) -> str:
    """Normalize math string for comparison."""
    s = s.strip()
    # Remove trailing period
    s = s.rstrip(".")
    # Remove \boxed{...}
    s = re.sub(r'\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}', r'\1', s)
    # Remove dollar signs
    s = s.replace("$", "")
    # Normalize whitespace
    s = " ".join(s.split())
    return s


def _try_float(s: str) -> float | None:
    """Try to parse string as float."""
    s = s.strip().replace(",", "").replace(" ", "")
    # Handle fractions like "1/3"
    m = re.fullmatch(r'(-?\d+)/(\d+)', s)
    if m:
        try:
            return int(m.group(1)) / int(m.group(2))
        except ZeroDivisionError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _latex_to_sympy_str(s: str) -> str:
    """Convert common LaTeX to sympy-parseable string."""
    s = _clean(s)
    s = s.replace("\\frac{", "(").replace("}{", ")/(").replace("}", ")")
    s = s.replace("\\sqrt{", "sqrt(")
    s = s.replace("\\pi", "pi")
    s = s.replace("\\cdot", "*")
    s = s.replace("\\times", "*")
    s = s.replace("\\div", "/")
    s = s.replace("\\left", "").replace("\\right", "")
    s = s.replace("^", "**")
    # Clean remaining backslashes
    s = re.sub(r'\\[a-zA-Z]+', '', s)
    # Fix adjacent parens: )(  -> )*(
    s = re.sub(r'\)\(', ')*(', s)
    return s


def _sympy_equiv(pred_s: str, gold_s: str) -> bool:
    """Check symbolic equivalence using sympy."""
    try:
        from sympy import sympify, simplify, Rational
        from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication

        transforms = standard_transformations + (implicit_multiplication,)

        pred_expr = parse_expr(_latex_to_sympy_str(pred_s), transformations=transforms)
        gold_expr = parse_expr(_latex_to_sympy_str(gold_s), transformations=transforms)

        # Direct equality
        if pred_expr == gold_expr:
            return True

        # Simplified equality
        diff = simplify(pred_expr - gold_expr)
        if diff == 0:
            return True

        # Numeric evaluation
        try:
            pred_val = complex(pred_expr.evalf())
            gold_val = complex(gold_expr.evalf())
            if abs(pred_val - gold_val) < 1e-6 * max(abs(gold_val), 1):
                return True
        except (ValueError, TypeError):
            pass

        return False
    except Exception:
        return False


def verify_math(pred: str, gold: str) -> float:
    """Verify math answer with cascade: string → numeric → symbolic.

    Returns:
        1.0 = correct
        0.0 = incorrect
    """
    if not pred or not gold:
        return 0.0

    pred_clean = _clean(pred)
    gold_clean = _clean(gold)

    # 1. Exact string match
    if pred_clean.lower() == gold_clean.lower():
        return 1.0

    # Remove spaces for tighter comparison
    if pred_clean.replace(" ", "").lower() == gold_clean.replace(" ", "").lower():
        return 1.0

    # 2. Multiple choice (single letter)
    if re.fullmatch(r'[A-Ea-e]', gold_clean):
        if pred_clean.upper() == gold_clean.upper():
            return 1.0
        # Check if pred contains "answer is X" pattern
        m = re.search(r'(?:answer|option)\s*(?:is\s*)?([A-Ea-e])', pred, re.IGNORECASE)
        if m and m.group(1).upper() == gold_clean.upper():
            return 1.0
        return 0.0

    # 3. Numeric equivalence
    pred_f = _try_float(pred_clean)
    gold_f = _try_float(gold_clean)
    if pred_f is not None and gold_f is not None:
        if gold_f == 0:
            return 1.0 if abs(pred_f) < 1e-9 else 0.0
        if abs(pred_f - gold_f) < 1e-6 * max(abs(gold_f), 1):
            return 1.0
        return 0.0

    # 4. Symbolic equivalence (LaTeX / expressions)
    if _sympy_equiv(pred_clean, gold_clean):
        return 1.0

    return 0.0
