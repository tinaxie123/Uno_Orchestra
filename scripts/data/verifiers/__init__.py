from __future__ import annotations

from scripts.data.verifiers.math_verifier import verify_math
from scripts.data.verifiers.qa_verifier import verify_qa
from scripts.data.verifiers.code_verifier import verify_code
from scripts.data.verifiers.toolace_call_verifier import verify_toolace as _verify_toolace_bfcl


def verify_toolace(pred: str, gold: str) -> bool:
    # Binary adapter for the boolean `VERIFIERS` registry. Training /
    # reward paths that want the float score should import from
    # `toolace_call_verifier` directly.
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


def verify(pred: str, gold: str, source: str) -> bool:
    """Route to the appropriate verifier based on source dataset."""
    for key, fn in VERIFIERS.items():
        if key in source:
            return fn(pred, gold)
    return False
