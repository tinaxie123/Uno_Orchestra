"""Math answer verifier for GSM8K and NuminaMath.

Verification strategy:
1. Extract numeric value from both pred and gold (strip $, %, commas)
2. If both parse as floats, compare with tolerance 1e-3
3. If numeric comparison fails, try LaTeX-normalized string match
4. Handle common math formats: fractions, scientific notation, expressions
"""

from __future__ import annotations

import re


def _strip_math_formatting(s: str) -> str:
    """Remove common math formatting characters."""
    s = s.strip()
    # Remove trailing period or comma
    s = s.rstrip('.,')
    # Remove dollar signs, percent, commas in numbers
    s = re.sub(r'[$%,]', '', s)
    # Remove leading/trailing whitespace again
    return s.strip()


def _try_parse_number(s: str) -> float | None:
    """Try to parse a string as a number, handling fractions and scientific notation."""
    s = _strip_math_formatting(s)

    # Direct float parse
    try:
        return float(s)
    except ValueError:
        pass

    # LaTeX fraction: \frac{a}{b}
    m = re.match(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', s)
    if m:
        try:
            return float(m.group(1)) / float(m.group(2))
        except (ValueError, ZeroDivisionError):
            pass

    # Plain fraction: a/b
    m = re.match(r'^(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)$', s)
    if m:
        try:
            return float(m.group(1)) / float(m.group(2))
        except (ValueError, ZeroDivisionError):
            pass

    # Mixed number: a b/c
    m = re.match(r'^(-?\d+)\s+(\d+)\s*/\s*(\d+)$', s)
    if m:
        try:
            whole = float(m.group(1))
            frac = float(m.group(2)) / float(m.group(3))
            return whole + frac if whole >= 0 else whole - frac
        except (ValueError, ZeroDivisionError):
            pass

    return None


def _normalize_latex(s: str) -> str:
    """Normalize LaTeX expressions for string comparison."""
    s = s.strip()
    # Remove \left, \right
    s = re.sub(r'\\(left|right)', '', s)
    # Remove \, spacing
    s = s.replace('\\,', '')
    # Normalize whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def verify_math(pred: str, gold: str) -> bool:
    """Verify math answer: numeric comparison with fallback to string match.

    Args:
        pred: Model's predicted answer
        gold: Gold standard answer (from #### for GSM8K, \\boxed{} for NuminaMath)

    Returns:
        True if the answer is correct
    """
    if not pred or not gold:
        return False

    # Try numeric comparison
    pred_num = _try_parse_number(pred)
    gold_num = _try_parse_number(gold)

    if pred_num is not None and gold_num is not None:
        # Both are numbers: compare with tolerance
        if gold_num == 0:
            return abs(pred_num) < 1e-6
        return abs(pred_num - gold_num) / max(abs(gold_num), 1e-10) < 1e-3

    # Fallback: normalized LaTeX string match
    p_norm = _normalize_latex(pred)
    g_norm = _normalize_latex(gold)
    if p_norm == g_norm:
        return True

    # Last resort: strip all non-alphanumeric and compare
    p_stripped = re.sub(r'[^a-zA-Z0-9]', '', pred.lower())
    g_stripped = re.sub(r'[^a-zA-Z0-9]', '', gold.lower())
    return p_stripped == g_stripped and len(p_stripped) > 0
