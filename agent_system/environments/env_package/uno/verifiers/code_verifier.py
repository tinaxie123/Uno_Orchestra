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

import sys as _sys
import types as _types
from typing import Any

# `verl.utils.reward_score.prime_code` pulls in `pyext`, which is
# py2-era and fails to build under Python 3.12. Without a shim, every
# code row hits ImportError → outer except → False (silent zero).
# Install a 5-line stdlib shim BEFORE any lazy import below so PRIME's
# testing_util.RuntimeModule.from_string("tmp_sol", "", sol) keeps the
# same semantics: compile a string of source into a fresh module object.
if "pyext" not in _sys.modules:
    _shim = _types.ModuleType("pyext")

    class _RuntimeModule:
        @staticmethod
        def from_string(name, _doc, source):
            m = _types.ModuleType(name)
            exec(compile(source, f"<{name}>", "exec"), m.__dict__)
            return m

    _shim.RuntimeModule = _RuntimeModule
    _sys.modules["pyext"] = _shim


def verify_code(pred: str, gold: str, tests: Any = None) -> bool:
    if not pred:
        return False

    # The parquet builder ships tests with `inputs`/`outputs` as numpy
    # ndarrays (object dtype). Truthiness checks on multi-element arrays
    # (`if arr:`) raise ValueError, which silently zeros 57% of code rows
    # via the upstream try/except. Use explicit None+len checks instead,
    # then coerce to plain lists before handing off to PRIME's harness.
    if not isinstance(tests, dict):
        return False
    ins = tests.get("inputs")
    outs = tests.get("outputs")
    if ins is None or outs is None or len(ins) == 0 or len(outs) == 0:
        return False

    try:
        from verl.utils.reward_score.prime_code import (
            compute_score as _prime_compute_score,
        )
        normalized_tests = {"inputs": list(ins), "outputs": list(outs)}
        success, _meta = _prime_compute_score(pred, normalized_tests)
        return bool(success)
    except Exception:
        return False
