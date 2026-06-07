"""Math answer verifier for GSM8K and NuminaMath.

Follows OpenCompass evaluation logic:
1. Extract the last number from model output (handles "The answer is 42." → "42")
2. Extract gold answer (#### for GSM8K, \\boxed{} for NuminaMath)
3. Numeric comparison with tolerance 1e-6
4. Fallback: LaTeX-normalized string match
"""

from __future__ import annotations

import re


def _extract_last_number(text: str) -> str | None:
    """Extract the last number from text (enhanced OpenCompass gsm8k_postprocess).

    Priority order:
    1. "answer is X" / "answer: X" / "= X" sentence patterns
    2. Last number in text (supporting comma-separated thousands)
    """
    # Truncate at "Question:" to avoid few-shot leakage
    text = text.split('Question:')[0]

    # Priority 1: answer sentence patterns — often more reliable than last number
    # Only safe, high-precision patterns to avoid false matches
    _NUM = r'[-]?\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?'
    answer_patterns = [
        # "the answer is 42", "answer is $448,000"
        r'(?:the\s+)?answer\s+is\s*[:=\s]*(' + _NUM + r')',
        # "makes/earns a profit of $448,000"
        r'(?:makes|earns|gets|saves|spends)\s+(?:a\s+)?(?:total|profit|sum|cost)\s+of\s+(' + _NUM + r')',
        # "There are 18", "there is 1"
        r'[Tt]here\s+(?:are|is|were|was)\s+(' + _NUM + r')',
        # "= $448,000." or "= 72" at end of sentence
        r'=\s*(' + _NUM + r')\s*[.;,]?\s*$',
    ]
    for pat in answer_patterns:
        matches = re.findall(pat, text, re.IGNORECASE | re.MULTILINE)
        if matches:
            return matches[-1].replace(',', '').replace('$', '')

    # Priority 2: last number (with comma-separated thousands support)
    numbers = re.findall(r'-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d+|-?\d+', text)
    if numbers:
        return numbers[-1].replace(',', '')


def _strip_math_formatting(s: str) -> str:
    """Remove common math formatting characters."""
    s = s.strip()
    s = s.rstrip('.,')
    s = re.sub(r'[$%,]', '', s)
    return s.strip()


def _strip_latex_wrappers(s: str) -> str:
    """Strip LaTeX text wrappers: \\text{B} → B, \\textbf{(D)} → (D), etc."""
    s = re.sub(r'\\(?:text|textbf|textit|textrm|mathrm|mathbf)\{([^{}]*)\}', r'\1', s)
    # Remove \( ... \) wrappers
    s = re.sub(r'\\\((.+?)\\\)', r'\1', s)
    # Remove \: \; \, \! \  spacing commands (including backslash-space)
    s = re.sub(r'\\[,:;! ]', '', s)
    return s.strip()


def _extract_choice_letter(s: str) -> str | None:
    """Extract choice letter (A-E) from answer text."""
    s = _strip_latex_wrappers(s).strip()
    # Exact single letter
    if re.fullmatch(r'[A-Ea-e]', s.strip()):
        return s.strip().upper()
    # Leading (A) or A followed by separator or content
    m = re.match(r'^\(?([A-Ea-e])\)?(?:[\s.,:;]|$)', s)
    if m:
        return m.group(1).upper()
    # (A) followed by anything (e.g. "(B)2\sqrt{43}")
    m = re.match(r'^\(([A-Ea-e])\)', s)
    if m:
        return m.group(1).upper()
    return None




def _try_parse_number(s: str) -> float | None:
    """Try to parse a string as a number, handling fractions and scientific notation."""
    s = _strip_math_formatting(s)

    # Direct float parse
    try:
        return float(s)
    except ValueError:
        pass

    # LaTeX fraction: \frac{a}{b}
    m = re.match(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', s) or re.match(r'\\\\frac\{([^{}]+)\}\{([^{}]+)\}', s)
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
    s = re.sub(r'\\\\?(left|right)', '', s)
    s = re.sub(r'\\\\?(tfrac|dfrac)', r'\\frac', s)
    s = s.replace('\\,', '')
    s = s.replace('\\cdot', '*')
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _parse_interval(s: str) -> tuple[str, float, float, str] | None:
    """Parse interval like [0, 1/2], (0, \\frac{1}{2}], etc. Returns (left_bracket, lo, hi, right_bracket)."""
    s = _normalize_latex(s).strip()
    m = re.match(r'^([(\[])(.+?),\s*(.+?)([)\]])$', s)
    if not m:
        return None
    lb, lo_s, hi_s, rb = m.group(1), m.group(2), m.group(3), m.group(4)
    lo = _try_parse_number(lo_s)
    hi = _try_parse_number(hi_s)
    if lo is None or hi is None:
        return None
    return lb, lo, hi, rb


def _try_compare_interval(pred: str, gold: str) -> bool | None:
    """Compare two interval expressions. Returns None if neither is an interval."""
    g_iv = _parse_interval(gold)
    p_iv = _parse_interval(pred)
    if g_iv is None and p_iv is None:
        return None
    if g_iv is None or p_iv is None:
        return False
    return (g_iv[0] == p_iv[0] and g_iv[3] == p_iv[3]
            and abs(g_iv[1] - p_iv[1]) < 1e-6
            and abs(g_iv[2] - p_iv[2]) < 1e-6)


def _nums_close(a: float, b: float) -> bool:
    """Check if two numbers match within numeric tolerance."""
    if b == 0:
        return abs(a) < 1e-6
    return abs(a - b) / max(abs(b), 1e-10) < 1e-6


def verify_math(pred: str, gold: str) -> bool:
    """Verify math answer following OpenCompass logic + enhanced matching.

    1. Choice-letter match (A/B/C/D/E)
    2. Direct numeric comparison on raw pred/gold
    3. Expression eval (handles '27000-16000' → 11000)
    4. Extract last number from pred (OpenCompass style)
    5. LaTeX boxed/frac extraction
    6. Normalized LaTeX string match
    7. Strip all non-alphanumeric and compare
    """
    pred, gold = str(pred), str(gold)
    if not pred or not gold:
        return False

    # --- Pre-strip LaTeX wrappers on both sides ---
    pred = _strip_latex_wrappers(pred).strip() or pred.strip()
    gold = _strip_latex_wrappers(gold).strip() or gold.strip()

    # --- Pass 0a: choice letter match ---
    pred_letter = _extract_choice_letter(pred)
    gold_letter = _extract_choice_letter(gold)
    if pred_letter and gold_letter:
        return pred_letter == gold_letter

    # --- Pass 0b: exact match after strip ---
    if pred == gold:
        return True

    # --- Pass 0c: interval / set comparison ---
    interval = _try_compare_interval(pred, gold)
    if interval is not None:
        return interval

    # --- Pass 1: direct numeric comparison ---
    pred_num = _try_parse_number(pred) or _try_parse_number(_normalize_latex(pred))
    gold_num = _try_parse_number(gold) or _try_parse_number(_normalize_latex(gold))

    if pred_num is not None and gold_num is not None:
        if _nums_close(pred_num, gold_num):
            return True

    # --- Pass 2: extract last number from both pred and gold ---
    pred_last = _extract_last_number(pred)
    gold_last = _extract_last_number(gold)
    p_num = _try_parse_number(pred_last) if pred_last else pred_num
    g_num = _try_parse_number(gold_last) if gold_last else gold_num
    if p_num is not None and g_num is not None:
        if _nums_close(p_num, g_num):
            return True

    # --- Pass 2b: extract LaTeX expression from pred ---
    boxed = re.findall(r'\\\\?boxed\{((?:[^{}]|\{[^{}]*\})*)\}', pred)
    if boxed:
        boxed_num = _try_parse_number(boxed[-1])
        if gold_num is not None and boxed_num is not None:
            if _nums_close(boxed_num, gold_num):
                return True
        if _normalize_latex(boxed[-1]) == _normalize_latex(gold):
            return True

    frac_matches = re.findall(r'\\\\?frac\{[^{}]*\}\{[^{}]*\}', pred)
    if frac_matches and gold_num is not None:
        frac_num = _try_parse_number(frac_matches[-1])
        if frac_num is not None and _nums_close(frac_num, gold_num):
            return True

    # --- Pass 2c: evaluate simple arithmetic expressions ---
    if gold_num is not None and pred_num is None:
        # pred might be an unevaluated expression like "75+60"
        try:
            evaled = float(eval(pred.strip(), {"__builtins__": {}}, {}))
            if _nums_close(evaled, gold_num):
                return True
        except Exception:
            pass

    # --- Pass 3: normalized LaTeX string match ---
    p_norm = _normalize_latex(pred)
    g_norm = _normalize_latex(gold)
    if p_norm == g_norm:
        return True

    # --- Pass 4: strip all non-alphanumeric and compare ---
    p_stripped = re.sub(r'[^a-zA-Z0-9]', '', pred.lower())
    g_stripped = re.sub(r'[^a-zA-Z0-9]', '', gold.lower())
    return p_stripped == g_stripped and len(p_stripped) > 0
