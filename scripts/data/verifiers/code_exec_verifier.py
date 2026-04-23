"""
Execution-based code verifier for TACO tasks.

Strategy:
1. Extract code from prediction (```python blocks or raw code)
2. Run both gold code and pred code in subprocess with example inputs
3. Compare outputs

For RL reward: returns float in [0, 1]
  - 1.0 = all test cases pass
  - 0.5 = code runs but wrong output
  - 0.0 = code doesn't run / no code found
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import os
import signal


EXEC_TIMEOUT = 10  # seconds per test case
MAX_OUTPUT = 5000  # chars


def _extract_code(text: str) -> str | None:
    """Extract Python code from model output."""
    # Pattern 1: ```python ... ```
    blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', text, re.DOTALL)
    if blocks:
        return blocks[-1].strip()

    # Pattern 2: code that looks like a solution (has def/import/for/print/input)
    lines = []
    in_code = False
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped.startswith(('import ', 'from ', 'def ', 'class ', 'for ', 'while ',
                                'n ', 'n=', 'print(', 'input(', 'sys.', 'if ', 'try:')):
            in_code = True
        if in_code:
            lines.append(line)
        if in_code and stripped == '' and len(lines) > 3:
            break

    if lines and len('\n'.join(lines)) > 10:
        return '\n'.join(lines)

    # Pattern 3: entire text is executable code (single line or short)
    text_stripped = text.strip()
    if text_stripped and not text_stripped.startswith(('The ', 'I ', 'We ', 'This ')):
        try:
            compile(text_stripped, '<test>', 'exec')
            return text_stripped
        except SyntaxError:
            pass

    return None


def _extract_gold_code(gold: str) -> str | None:
    """Extract code from TACO gold answer format: ["code..."] or just code."""
    if gold.startswith('[') and gold.endswith(']'):
        try:
            import json
            codes = json.loads(gold)
            if codes and isinstance(codes[0], str):
                return codes[0]
        except Exception:
            pass
    if 'def ' in gold or 'import ' in gold or 'for ' in gold:
        return gold
    return None


def _extract_test_io_from_question(question: str) -> list[tuple[str, str]]:
    """Extract example input/output pairs from question text.

    TACO questions typically have:
        Examples
        Input
        3
        1 2 3
        Output
        6
    """
    tests = []
    # Find Input/Output blocks
    pattern = r'(?:Input|input)\s*\n(.*?)(?:Output|output)\s*\n(.*?)(?=\n\s*(?:Input|input|Example|Note|\Z))'
    matches = re.findall(pattern, question, re.DOTALL)
    for inp, out in matches:
        inp = inp.strip()
        out = out.strip()
        if inp and out and len(inp) < 2000 and len(out) < 2000:
            tests.append((inp, out))
    return tests[:5]  # Max 5 test cases


def _run_code(code: str, stdin: str = "", timeout: int = EXEC_TIMEOUT) -> tuple[str, int]:
    """Execute Python code in subprocess, return (stdout, exit_code)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        result = subprocess.run(
            ['python3', tmp],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=lambda: signal.alarm(timeout),
        )
        output = result.stdout[:MAX_OUTPUT].strip()
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return "", -1
    except Exception as e:
        return str(e)[:200], -1
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def verify_code_exec(pred: str, gold: str, question: str = "") -> float:
    """Execute-based code verification. Binary: 1.0 or 0.0.

    Args:
        pred: Model's predicted answer (may contain code)
        gold: Gold answer (TACO format: ["code"] or code string)
        question: Original question text (used to extract test cases)

    Returns:
        1.0 = all test cases pass (or code output matches gold)
        0.0 = anything else
    """
    pred_code = _extract_code(pred)
    if not pred_code:
        return 0.0

    # Strategy 1: If question has example I/O, test against all of them
    tests = _extract_test_io_from_question(question) if question else []
    if tests:
        for inp, expected_out in tests:
            actual_out, exit_code = _run_code(pred_code, stdin=inp)
            if exit_code != 0 or not _outputs_match(actual_out, expected_out):
                return 0.0  # Any failure → 0
        return 1.0  # All passed

    # Strategy 2: Compare pred vs gold code outputs
    gold_code = _extract_gold_code(gold)
    if gold_code:
        test_input = ""
        gold_out, gold_exit = _run_code(gold_code, stdin=test_input, timeout=5)
        if gold_exit == 0 and gold_out:
            pred_out, pred_exit = _run_code(pred_code, stdin=test_input, timeout=5)
            if pred_exit == 0 and _outputs_match(pred_out, gold_out):
                return 1.0
            return 0.0

    # No test cases and no gold code to compare → can't verify
    return 0.0


def _outputs_match(actual: str, expected: str) -> bool:
    """Compare outputs with tolerance for whitespace."""
    a = actual.strip().split()
    b = expected.strip().split()
    return a == b


