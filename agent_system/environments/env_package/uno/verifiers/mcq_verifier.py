"""Strict letter-match verifier for multiple-choice rows.

After scripts/data/rebuild_mcq_choices.py runs, every MCQ row's gold is a
single letter (A-G) and the prompt embeds the choices and asks the model
to emit one letter inside <final_answer>. This verifier extracts that
letter from the prediction and compares it strictly to the gold letter.

We deliberately do NOT use verify_qa here because:
- verify_qa strips "a/an/the" → gold "A" becomes empty string.
- verify_qa's `g_norm in p_norm` fires false-positives like "valid" ⊂
  "invalid" or "meat" ⊂ "meatball".
- f1≥0.5 on token overlap rewards near-misses we don't want for MCQ.

Extraction strategy (most-specific-first):
    1. Whole prediction is one letter (with optional parens / period)
    2. "answer/choice/option (is|:|=) X" — strongest intent marker
    3. Trailing single-letter token at end of string
    4. (X) or [X] wrapped letter — RIGHTMOST match, since the model
       typically lists choices "(A) ... (B) ..." then concludes with the
       chosen one. Picking the leftmost would return the first option
       referenced rather than the answer.
    5. Last-resort: rightmost standalone single-letter token.

All regexes are case-insensitive — the model occasionally emits "(b)" /
"the answer is c" lowercase even though SFT trained on uppercase, and a
case-sensitive miss here costs a true-positive reward signal.

If extraction fails, return False.
"""
from __future__ import annotations

import re

# Unicode set of valid choice letters (max we issue: G — bbh
# logical_deduction_seven_objects has A-G).
_VALID_LETTERS = set("ABCDEFG")

# "answer/choice/option (is|:|=) X" — strongest intent marker, run before
# any positional fallback so it wins over an earlier "(A)" reference.
_ANSWER_KEYWORD = re.compile(
    r"(?:answer|choice|option)\s*(?:is|:|=)\s*\(?\s*([A-G])\s*\)?",
    re.IGNORECASE,
)
# Whole-string single letter (with optional parens / trailing period)
_WHOLE_LETTER = re.compile(r"\(?\s*([A-G])\s*\)?\s*\.?", re.IGNORECASE)
# Trailing single-letter token at end of string
_TRAILING_LETTER = re.compile(r"\b([A-G])\b\s*\.?\s*$", re.IGNORECASE)
# (X) or [X] wrapped — used with findall to take the RIGHTMOST match
_WRAPPED_LETTER = re.compile(r"[\(\[]\s*([A-G])\s*[\)\]]", re.IGNORECASE)
# Any standalone single-letter token — used with findall, rightmost match
_STANDALONE_LETTER = re.compile(r"\b([A-G])\b", re.IGNORECASE)


def _extract_letter(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()

    # 1. Whole prediction is just one letter (covers "B", "(b)", "B.", "(B).")
    if len(s) <= 4:
        m = _WHOLE_LETTER.fullmatch(s)
        if m:
            return m.group(1).upper()

    # 2. Strongest intent marker — "answer is X", "the answer: X", etc.
    # Use findall + rightmost so a model self-correction like
    # "the answer is A. Actually the answer is B." resolves to B, not A.
    keyword_hits = _ANSWER_KEYWORD.findall(s)
    if keyword_hits:
        return keyword_hits[-1].upper()

    # 3. Trailing single letter (model concluded with a bare letter)
    m = _TRAILING_LETTER.search(s)
    if m:
        return m.group(1).upper()

    # 4. (X) / [X] wrapped — rightmost match (skip earlier "(A) is wrong" refs)
    wrapped = _WRAPPED_LETTER.findall(s)
    if wrapped:
        return wrapped[-1].upper()

    # 5. Last-resort: rightmost standalone letter
    standalone = _STANDALONE_LETTER.findall(s)
    if standalone:
        return standalone[-1].upper()

    return None


def verify_mcq(pred: str, gold: str) -> bool:
    if not gold:
        return False
    g = gold.strip().upper()
    # Unwrap "(D)" → "D" if dataset already canonicalised that way slipped in
    m = re.fullmatch(r"\(?\s*([A-G])\s*\)?", g)
    if not m:
        return False
    g_letter = m.group(1)

    p_letter = _extract_letter(pred or "")
    return p_letter == g_letter
