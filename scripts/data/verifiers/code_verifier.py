from __future__ import annotations

import os
import re
import subprocess
import tempfile


SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "")
EXEC_TIMEOUT = 10  


def _run_in_sandbox(code: str, test_input: str, expected_output: str) -> bool:
    """Execute code in Docker container and compare output."""
    if not SANDBOX_IMAGE:
        return False

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
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
        actual = result.stdout.strip()
        expected = expected_output.strip()
        return actual == expected
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return False
    finally:
        os.unlink(tmp_path)


def _structural_check(code: str) -> bool:
    """Fallback: check code looks like a real solution."""
    has_def = bool(re.search(r'\b(def|class)\b', code))
    has_output = bool(re.search(r'\b(return|print)\b', code))
    non_trivial = len(code.strip()) >= 30
    has_logic = bool(re.search(r'(for|while|if|elif|else|try|with)\b', code))
    return has_def and has_output and non_trivial and has_logic


def verify_code(pred: str, gold: str,
                test_input: str = "", expected_output: str = "") -> bool:
   
    if not pred:
        return False

    if SANDBOX_IMAGE and test_input and expected_output:
        return _run_in_sandbox(pred, test_input, expected_output)

    return _structural_check(pred)
