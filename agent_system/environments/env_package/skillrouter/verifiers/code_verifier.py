"""Code verifier: delegate to verl's PRIME-code harness.

PRIME's `compute_score` runs the student's code in a forked subprocess with
signal-based timeouts, recursion limit bumps, and the standard APPS/TACO
import preamble. It handles stdin/stdout problems directly; fn_name
(call-based) problems are out-of-scope and fall back to 0.

Tests format (injected into env_kwargs["tests"] by the parquet builder):
    {"inputs": [str, ...], "outputs": [str, ...]}    # stdin/stdout

When `tests` is missing, we deliberately return False instead of running
a structural check. The earlier structural fallback (any string with
`def` + `return` + `for/if` + ≥30 chars → True) gave the RL policy a
trivial proxy reward that rewarded emitting plausible-looking code
regardless of correctness. Returning False forces the upstream RL
parquet builder to actually pipe the TACO input/output test cases
through env_kwargs["tests"].
"""
from __future__ import annotations

from typing import Any

# `verl.utils.reward_score.prime_code` pulls in `pyext`, which is
# py2-era and fails to build under Python 3.12. Import it lazily so
# merely importing this module (e.g. for registry-side-effect in the
# rollout) doesn't explode on the pyext compat gap. We only need it
# when a row actually ships runnable stdin/stdout tests.


def verify_code(pred: str, gold: str, tests: Any = None) -> bool:
    if not pred:
        return False

    if isinstance(tests, dict) and tests.get("inputs") and tests.get("outputs"):
        try:
            from verl.utils.reward_score.prime_code import (
                compute_score as _prime_compute_score,
            )
            success, _meta = _prime_compute_score(pred, tests)
            return bool(success)
        except Exception:
            return False

    # No runnable tests → we cannot verify the code. Refuse to guess.
    return False
