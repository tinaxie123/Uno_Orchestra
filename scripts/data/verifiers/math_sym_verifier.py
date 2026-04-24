"""
Symbolic math verifier for NuminaMath / MATH-style tasks.

Primary path: Hendrycks MATH `is_equiv` (vendored at
`verl.utils.reward_score.math`), which is the reference scorer used by
lm-evaluation-harness. We add two thin wrappers on top:

  1. `_clean` — strip `\\boxed{...}`, dollar signs, trailing period, and
     normalize whitespace BEFORE handing strings to `is_equiv`. Hendrycks
     expects the caller to pre-extract boxed content; in our pipeline the
     model is encouraged to emit a final answer that may or may not be
     boxed, so we always unbox here.

  2. Numeric fallback — if Hendrycks string-normalization says "not
     equivalent", try float comparison (handles cases like "1/3" vs
     "0.333..." that Hendrycks won't unify).

Returns float in {0.0, 1.0}.
"""
from __future__ import annotations

import os
import re
import sys

# Vendored `verl` lives at the repo root.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from verl.utils.reward_score.math_reward import is_equiv as _hendrycks_is_equiv


def _clean(s: str) -> str:
    """Strip boxed wrapper, dollar signs, trailing period, extra whitespace."""
    s = s.strip().rstrip(".")
    # Unwrap \boxed{...} (possibly with one level of nested braces).
    s = re.sub(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}", r"\1", s)
    s = s.replace("$", "")
    return " ".join(s.split())


def _try_float(s: str) -> float | None:
    s = s.strip().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(-?\d+)/(\d+)", s)
    if m:
        try:
            return int(m.group(1)) / int(m.group(2))
        except ZeroDivisionError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def verify_math(pred: str, gold: str) -> float:
    """Binary correctness for math answers. Returns 1.0 or 0.0."""
    if not pred or not gold:
        return 0.0

    pred_clean = _clean(pred)
    gold_clean = _clean(gold)

    # Primary: Hendrycks string-normalized equivalence.
    if _hendrycks_is_equiv(pred_clean, gold_clean):
        return 1.0

    # Numeric fallback for "1/3" vs "0.333...", etc.
    pf, gf = _try_float(pred_clean), _try_float(gold_clean)
    if pf is not None and gf is not None:
        if gf == 0:
            return 1.0 if abs(pf) < 1e-9 else 0.0
        if abs(pf - gf) < 1e-6 * max(abs(gf), 1.0):
            return 1.0

    return 0.0
