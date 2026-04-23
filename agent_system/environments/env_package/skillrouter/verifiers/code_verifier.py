"""Code verifier: delegate to verl's PRIME-code harness.

PRIME's `compute_score` runs the student's code in a forked subprocess with
signal-based timeouts, recursion limit bumps, and the standard APPS/TACO
import preamble. It handles stdin/stdout problems directly; fn_name
(call-based) problems are out-of-scope and fall back to 0.

Tests format (injected into env_kwargs["tests"] by the parquet builder):
    {"inputs": [str, ...], "outputs": [str, ...]}    # stdin/stdout

When `tests` is missing (rare — TACO row with no usable IO), we fall back
to the loose structural check so the reward signal is at least non-zero
for code-shaped outputs.
"""
from __future__ import annotations

import re
from typing import Any

from verl.utils.reward_score.prime_code import compute_score as _prime_compute_score


def _structural_check(code: str) -> bool:
    has_def = bool(re.search(r'\b(def|class)\b', code))
    has_output = bool(re.search(r'\b(return|print)\b', code))
    has_logic = bool(re.search(r'(for|while|if|elif|else|try|with)\b', code))
    return has_def and has_output and len(code.strip()) >= 30 and has_logic


def verify_code(pred: str, gold: str, tests: Any = None) -> bool:
    if not pred:
        return False

    if isinstance(tests, dict) and tests.get("inputs") and tests.get("outputs"):
        try:
            success, _meta = _prime_compute_score(pred, tests)
            return bool(success)
        except Exception:
            return False

    return _structural_check(pred)
