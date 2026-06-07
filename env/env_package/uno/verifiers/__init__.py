from __future__ import annotations

from .math_verifier import verify_math
from .qa_verifier import verify_qa
from .code_verifier import verify_code
from .mcq_verifier import verify_mcq


def verify_toolace(pred: str, gold: str) -> bool:
    """Lightweight ToolACE/ToolBench verifier.

    The old implementation lived under scripts/data/, which is no longer part
    of the runtime package.  Keep evaluation importable by checking the core
    contract used by ToolACE-style examples: predicted function names must cover
    the gold function names. Argument validation is benchmark-adapter specific.
    """
    import json
    import re

    def _calls(text: str) -> list[str]:
        names: list[str] = []
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("tool_name") or obj.get("function")
                if isinstance(name, str) and name:
                    names.append(name.lower())
        if not names:
            names.extend(m.group(1).lower() for m in re.finditer(r"([A-Za-z_][\w.]*)\s*\(", text))
        return names

    gold_names = set(_calls(gold))
    if not gold_names:
        return verify_qa(pred, gold)
    pred_names = set(_calls(pred))
    return bool(gold_names and gold_names.issubset(pred_names))

# Iteration order matters: substring-matching returns the FIRST hit. Place
# more-specific keys before less-specific ones (e.g. `hendrycks_math` is a
# substring of nothing else; `quality` is short but no other source name
# contains "quality"). MCQ keys are explicit per-source so no collisions.
VERIFIERS: dict[str, callable] = {
    # math
    "gsm8k": verify_math,
    "numinamath": verify_math,
    "hendrycks_math": verify_math,        # alias: hendrycks_math_algebra / _intermediate_algebra / _number_theory
    "theoremqa": verify_math,             # numeric/symbolic answers; verify_math handles both
    # multi-hop / reading-comp QA
    "hotpotqa": verify_qa,                # also matches hotpotqa_fullwiki via substring
    "drop": verify_qa,
    "musique": verify_qa,                 # also matches musique_answerable via substring
    "2wikimultihopqa": verify_qa,
    # open-domain QA
    "nq_open": verify_qa,
    "triviaqa": verify_qa,                # matches triviaqa_nocontext via substring
    "webquestions": verify_qa,
    "quality": verify_qa,
    # short free-text labels: yes/no, true/false/unknown, valid/invalid, science answer
    "sciq": verify_qa,
    "strategyqa": verify_qa,
    "folio": verify_qa,
    "bbh_formal_fallacies": verify_qa,
    "legalbench": verify_qa,
    # multiple-choice (10 sources, post-rebuild_mcq_choices.py)
    # Gold canonicalised to a single letter (A-G); prompts now embed the
    # choices and instruct the model to emit one letter. verify_mcq does
    # strict letter extraction + match, no fuzzy logic.
    "arc_challenge": verify_mcq,
    "commonsenseqa": verify_mcq,
    "openbookqa": verify_mcq,
    "aqua_rat": verify_mcq,
    "mmlu_aux_stem": verify_mcq,
    "piqa": verify_mcq,
    "social_iqa": verify_mcq,
    "winogrande": verify_mcq,
    "logiqa2": verify_mcq,
    "bbh_logical_deduction": verify_mcq,
    # code
    "taco": verify_code,
    "codeforces_cots": verify_code,       # tests = {inputs, outputs} from prep
    "codecontests": verify_code,
    # tool-call
    "toolace": verify_toolace,
}

# Sources whose verifier needs `tests` threaded in via extras.
_CODE_KEYS = {"taco", "codeforces_cots", "codecontests"}


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
            if key in _CODE_KEYS:
                tests = (extras or {}).get("tests")
                return 1.0 if fn(pred, gold, tests=tests) else 0.0
            return 1.0 if fn(pred, gold) else 0.0
    return 0.0
