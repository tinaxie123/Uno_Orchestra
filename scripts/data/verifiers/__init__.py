"""Per-dataset answer verifiers for the data generation pipeline.

Each verifier takes (pred, gold) and returns bool.
The dispatch function `verify(pred, gold, source)` routes to the correct one.
"""

from __future__ import annotations

from scripts.data.verifiers.math_verifier import verify_math
from scripts.data.verifiers.qa_verifier import verify_qa
from scripts.data.verifiers.code_verifier import verify_code
from scripts.data.verifiers.toolace_verifier import verify_toolace

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
