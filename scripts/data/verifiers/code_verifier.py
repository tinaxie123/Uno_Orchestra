"""Sandboxed code verifier for the data-generation pipeline.

Used by scripts/data/generate_trajectories.py and friends to check a
teacher's candidate solution against a known stdin/stdout test pair.

When no test pair is provided we deliberately return False instead of
falling back to "does it look like code" — structural heuristics give
spurious 1s that would silently corrupt SFT data.
"""
from __future__ import annotations

import os
import subprocess
import tempfile


SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "")
EXEC_TIMEOUT = 10


def _run_in_sandbox(code: str, test_input: str, expected_output: str) -> bool:
    if not SANDBOX_IMAGE:
        return False

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--network=none",
             "--memory=256m", "--cpus=1",
             "-v", f"{tmp_path}:/solution.py:ro",
             SANDBOX_IMAGE,
             "python", "/solution.py"],
            input=test_input,
            capture_output=True, text=True,
            timeout=EXEC_TIMEOUT,
        )
        return result.stdout.strip() == expected_output.strip()
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return False
    finally:
        os.unlink(tmp_path)


def verify_code(pred: str, gold: str,
                test_input: str = "", expected_output: str = "") -> bool:
    if not pred:
        return False
    if SANDBOX_IMAGE and test_input and expected_output:
        return _run_in_sandbox(pred, test_input, expected_output)
    # No sandbox or no tests — cannot verify; refuse to guess.
    return False
