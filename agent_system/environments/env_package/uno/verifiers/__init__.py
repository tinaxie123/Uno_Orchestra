from __future__ import annotations

from .math_verifier import verify_math
from .qa_verifier import verify_qa
from .code_verifier import verify_code
# Single source of truth for ToolACE scoring lives under
# scripts/data/verifiers/. Repo root is on PYTHONPATH at train time.
from scripts.data.verifiers.toolace_call_verifier import verify_toolace as _verify_toolace_bfcl


def verify_toolace(pred: str, gold: str) -> bool:
    return _verify_toolace_bfcl(pred, gold, strict=True) >= 1.0

VERIFIERS: dict[str, callable] = {
    "gsm8k": verify_math,
    "numinamath": verify_math,
    "hotpotqa": verify_qa,
    "drop": verify_qa,
    "musique": verify_qa,
    "taco": verify_code,
    "toolace": verify_toolace,
}


def verify(pred: str, gold: str, source: str, extras: dict | None = None) -> float:
    """Route to the appropriate verifier based on source dataset.

    Returns 1.0 / 0.0 so callers can use the value directly as a reward.

    `extras` carries per-task artifacts the verifier may need (e.g. `tests`
    for code problems).
    """
    if not source:
        return 0.0
    source = source.lower()
    for key, fn in VERIFIERS.items():
        if key in source:
            if key == "taco":
                tests = (extras or {}).get("tests")
                return 1.0 if fn(pred, gold, tests=tests) else 0.0
            return 1.0 if fn(pred, gold) else 0.0
    return 0.0
