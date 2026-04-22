"""Error taxonomy analysis for all datasets across router and teacher trajectories.

Analyzes failure patterns in:
- Math: gsm8k, numinamath
- QA: drop, hotpotqa, musique
- Code: taco
- Tool: toolace

Usage:
    python scripts/data/error_taxonomy.py [--round round1] [--role both]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ── Verifier imports ──
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verifiers.math_verifier import verify_math, _try_parse_number, _extract_last_number, _strip_latex_wrappers, _extract_choice_letter
from verifiers.qa_verifier import verify_qa, _normalize_answer, _compute_f1
from verifiers.toolace_verifier import verify_toolace, _extract_function_calls

MATH_SOURCES = {"gsm8k", "numinamath"}
QA_SOURCES = {"drop", "hotpotqa", "musique"}
CODE_SOURCES = {"taco"}
TOOL_SOURCES = {"toolace"}


# ═══════════════════════════════════════════════════
#  Error classification functions
# ═══════════════════════════════════════════════════

def classify_math_error(pred: str, gold: str) -> str:
    """Classify a math prediction error into taxonomy categories."""
    if not pred or pred.strip() == "":
        return "empty_answer"

    # Choice letter
    pred_letter = _extract_choice_letter(pred)
    gold_letter = _extract_choice_letter(gold)
    if pred_letter and gold_letter and pred_letter != gold_letter:
        return "wrong_answer_choice_letter"

    # Try numeric parsing
    def to_num(s):
        n = _try_parse_number(s)
        if n is None:
            last = _extract_last_number(s)
            if last:
                n = _try_parse_number(last)
        return n

    p_num = to_num(pred)
    g_num = to_num(gold)

    # Check if pred is an unevaluated expression
    if re.match(r'^[\d\s\+\-\*\/\(\)\.]+$', pred.strip()) and any(op in pred for op in ['+', '-', '*', '/']):
        try:
            evaled = eval(pred.strip())
            if g_num is not None and abs(evaled - g_num) / max(abs(g_num), 1e-10) < 1e-4:
                return "wrong_answer_unevaluated_expr"
        except Exception:
            pass

    if p_num is not None and g_num is not None:
        if g_num == 0:
            if p_num == 0:
                return "wrong_answer_math_form"  # Same value, format diff
            return "wrong_answer_calculation"

        ratio = p_num / g_num if g_num != 0 else float('inf')
        diff_pct = abs(p_num - g_num) / max(abs(g_num), 1e-10)

        if diff_pct < 0.001:
            return "wrong_answer_math_form"  # Same value different format
        if diff_pct < 0.10:
            return "wrong_answer_rounding"
        if 1.95 < ratio < 2.05 or 0.48 < ratio < 0.52:
            return "wrong_answer_off_by_2x"
        if 9.5 < ratio < 10.5 or 0.095 < ratio < 0.105:
            return "wrong_answer_off_by_10x"
        if p_num == 0:
            return "wrong_answer_zero"
        return "wrong_answer_calculation"

    # One side is numeric, other isn't
    if g_num is not None and p_num is None:
        # pred has extra text
        pred_stripped = re.sub(r'[^0-9.\-]', '', pred)
        if pred_stripped:
            return "wrong_answer_extra_text"
        return "wrong_answer_math_form"

    # Both non-numeric — compare LaTeX
    p_stripped = _strip_latex_wrappers(pred).strip()
    g_stripped = _strip_latex_wrappers(gold).strip()
    if p_stripped.lower() == g_stripped.lower():
        return "verify_mismatch"

    # Check if pred is substring of gold or vice versa
    if g_stripped and p_stripped and (p_stripped in g_stripped or g_stripped in p_stripped):
        return "wrong_answer_partial"

    return "wrong_answer_math_form"


def classify_qa_error(pred: str, gold: str) -> str:
    """Classify a QA prediction error."""
    if not pred or pred.strip() == "":
        return "empty_answer"

    p_norm = _normalize_answer(pred)
    g_norm = _normalize_answer(gold)

    if not p_norm:
        return "empty_answer"

    # Check if pred is a refusal
    refusal_patterns = [
        r"i (?:cannot|can't|don't|do not|am unable)",
        r"(?:sorry|unfortunately),?\s+i",
        r"i need more (?:info|context|details)",
        r"(?:not enough|insufficient|missing) (?:info|context|data)",
        r"please (?:provide|check|refer)",
    ]
    for pat in refusal_patterns:
        if re.search(pat, pred.lower()):
            return "refusal_or_hedging"

    # Exact match after normalization (verifier bug)
    if p_norm == g_norm:
        return "verify_mismatch"

    # Gold contained in pred (over-verbose answer)
    if g_norm in p_norm and len(g_norm) > 2:
        return "verify_mismatch"  # QA verifier should catch this

    # F1 overlap
    f1 = _compute_f1(p_norm, g_norm)

    if f1 >= 0.5:
        # High overlap but verifier still failed — partial match
        return "wrong_answer_partial_overlap"

    # Check if it's a numeric QA
    try:
        p_num = float(pred.replace(',', '').strip())
        g_num = float(gold.replace(',', '').strip())
        if abs(p_num - g_num) / max(abs(g_num), 1e-10) < 0.10:
            return "wrong_answer_numeric_close"
        return "wrong_answer_numeric_far"
    except ValueError:
        pass

    # Check token overlap
    p_tokens = set(p_norm.split())
    g_tokens = set(g_norm.split())
    if p_tokens & g_tokens:
        return "wrong_answer_partial_overlap"

    # Complete miss — wrong entity
    if len(pred) > 200:
        return "wrong_answer_over_verbose"

    return "wrong_answer_wrong_entity"


def classify_code_error(pred: str, gold: str) -> str:
    """Classify a code prediction error."""
    if not pred or pred.strip() == "":
        return "empty_answer"

    # Check if pred is natural language instead of code
    code_indicators = ['def ', 'class ', 'import ', 'for ', 'while ', 'if ', 'return ', 'print(', '=']
    has_code = any(ind in pred for ind in code_indicators)

    if not has_code:
        if len(pred) < 20 and pred.strip().replace('-', '').replace('.', '').isdigit():
            return "wrong_answer_numeric_not_code"
        if any(phrase in pred.lower() for phrase in ['the program', 'the solution', 'the code', 'here is', 'i have']):
            return "wrong_answer_description_not_code"
        return "wrong_answer_not_code"

    # Code but possibly wrong
    # Check for trivial/skeleton code
    lines = [l for l in pred.strip().split('\n') if l.strip() and not l.strip().startswith('#')]
    if len(lines) < 3:
        return "wrong_answer_trivial_code"

    # Check if gold has test cases we can compare structure
    if 'def ' in pred and 'def ' in gold:
        # Both have function definitions — structural comparison
        pred_funcs = re.findall(r'def\s+(\w+)', pred)
        gold_funcs = re.findall(r'def\s+(\w+)', gold)
        if pred_funcs and gold_funcs and pred_funcs[0] != gold_funcs[0]:
            return "wrong_answer_wrong_function_name"

    return "wrong_answer_code_logic"


def classify_tool_error(pred: str, gold: str) -> str:
    """Classify a tool-use (toolace) prediction error."""
    if not pred or pred.strip() == "":
        return "empty_answer"

    # Check if pred is a refusal
    refusal_patterns = [
        r"i (?:cannot|can't|don't|do not|am unable)",
        r"(?:sorry|unfortunately),?\s+i",
        r"please (?:provide|check|refer|visit)",
        r"(?:not available|no access|unable to)",
        r"real-time (?:data|information|market)",
    ]
    for pat in refusal_patterns:
        if re.search(pat, pred.lower()):
            return "refusal_cannot_execute"

    # Check if pred contains tool calls
    pred_calls = _extract_function_calls(pred)
    gold_calls = _extract_function_calls(gold)

    if not pred_calls:
        # No tool calls in pred — model answered in natural language
        if len(pred) > 100:
            return "wrong_answer_nl_instead_of_tool"
        return "no_tool_call_in_answer"

    if not gold_calls:
        return "wrong_answer_other"  # Gold format unexpected

    pred_names = {c["name"].lower() for c in pred_calls}
    gold_names = {c["name"].lower() for c in gold_calls}

    if pred_names == gold_names:
        return "wrong_tool_arguments"
    if pred_names & gold_names:
        return "wrong_tool_partial_match"
    return "wrong_tool_completely"


def classify_trajectory_error(pred: str, gold: str, source: str,
                              complete: bool, n_delegates: int,
                              trajectory: dict) -> str:
    """Top-level error classifier that dispatches by domain."""
    # Infrastructure-level errors first
    if not complete:
        return "no_finish_or_incomplete"

    # Check for no tool call (direct finish with wrong answer)
    msgs = trajectory.get("messages", [])
    has_tool_call = False
    for m in msgs:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                if tc["function"]["name"] != "finish":
                    has_tool_call = True
                    break

    # Check for loops (repeated identical tool calls)
    tool_calls_list = []
    for m in msgs:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                tool_calls_list.append(tc["function"]["name"] + ":" + str(tc["function"]["arguments"])[:100])
    if len(tool_calls_list) > 4:
        # Check for repetition
        from collections import Counter as C
        tc_counts = C(tool_calls_list)
        if tc_counts.most_common(1)[0][1] >= 3:
            return "loop_or_stall"

    if not pred or pred.strip() == "":
        return "empty_answer"

    # Check instruction_missing_context
    missing_patterns = [
        r"(?:need|require|missing) (?:more )?(?:info|context|details|data)",
        r"(?:not enough|insufficient) (?:info|context)",
        r"cannot (?:be )?(?:determined|solved|answered)",
        r"(?:no|without) (?:enough )?(?:information|context)",
    ]
    for pat in missing_patterns:
        if re.search(pat, pred.lower()):
            return "instruction_missing_context"

    # Domain-specific classification
    if source in MATH_SOURCES:
        if not has_tool_call and n_delegates == 0:
            # Direct finish — check if it was a no_tool_call issue
            err = classify_math_error(pred, gold)
            if err == "wrong_answer_calculation" and not has_tool_call:
                return "no_tool_call_issued"  # Could solve with delegation
            return err
        return classify_math_error(pred, gold)
    elif source in QA_SOURCES:
        return classify_qa_error(pred, gold)
    elif source in CODE_SOURCES:
        return classify_code_error(pred, gold)
    elif source in TOOL_SOURCES:
        return classify_tool_error(pred, gold)
    else:
        return "unknown_source"


# ═══════════════════════════════════════════════════
#  Trajectory analysis
# ═══════════════════════════════════════════════════

def analyze_router_trajectories(path: Path) -> list[dict]:
    """Analyze router probe failures."""
    results = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if d["router_ok"]:
                continue
            for att in d.get("attempts", []):
                traj = att.get("trajectory", {})
                pred = traj.get("answer", "")
                complete = traj.get("complete", False)
                n_delegates = traj.get("n_delegates", 0)
                err = classify_trajectory_error(
                    str(pred), str(d["gold_answer"]), d["source"],
                    complete, n_delegates, traj
                )
                results.append({
                    "idx": d["idx"],
                    "source": d["source"],
                    "domain": d["domain"],
                    "role": "router",
                    "gold": str(d["gold_answer"])[:200],
                    "pred": str(pred)[:200],
                    "error_type": err,
                    "n_delegates": n_delegates,
                    "complete": complete,
                })
    return results


def analyze_teacher_trajectories(path: Path) -> list[dict]:
    """Analyze teacher trajectory failures."""
    results = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if d["teacher_ok"]:
                continue
            traj = d["trajectory"]
            pred = traj.get("answer", "")
            complete = traj.get("complete", False)
            n_delegates = traj.get("n_delegates", 0)
            err = classify_trajectory_error(
                str(pred), str(d["gold_answer"]), d["source"],
                complete, n_delegates, traj
            )
            results.append({
                "idx": d["idx"],
                "source": d["source"],
                "domain": d["domain"],
                "role": "teacher",
                "gold": str(d["gold_answer"])[:200],
                "pred": str(pred)[:200],
                "error_type": err,
                "n_delegates": n_delegates,
                "complete": complete,
            })
    return results


# ═══════════════════════════════════════════════════
#  Reporting
# ═══════════════════════════════════════════════════

def print_report(results: list[dict], title: str):
    """Print a formatted error taxonomy report."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

    if not results:
        print("  No failures found.")
        return

    # Overall
    error_counts = Counter(r["error_type"] for r in results)
    total = len(results)
    print(f"\n  Total failure attempts: {total}\n")
    print(f"  {'Error Type':<40} {'Count':>6} {'%':>7}")
    print(f"  {'-'*55}")
    for err, count in error_counts.most_common():
        print(f"  {err:<40} {count:>6} {count*100/total:>6.1f}%")

    # By source
    sources = sorted(set(r["source"] for r in results))
    for src in sources:
        src_results = [r for r in results if r["source"] == src]
        src_errors = Counter(r["error_type"] for r in src_results)
        src_total = len(src_results)
        print(f"\n  --- {src} ({src_total} failures) ---")
        print(f"  {'Error Type':<40} {'Count':>6} {'%':>7}")
        print(f"  {'-'*55}")
        for err, count in src_errors.most_common():
            print(f"  {err:<40} {count:>6} {count*100/src_total:>6.1f}%")

    # Sample cases per error type (top 3)
    print(f"\n  --- Sample Cases ---")
    for err, _ in error_counts.most_common(10):
        samples = [r for r in results if r["error_type"] == err][:3]
        print(f"\n  [{err}]")
        for s in samples:
            print(f"    idx={s['idx']}, src={s['source']}, gold={s['gold'][:60]}, pred={s['pred'][:60]}")


def generate_markdown_report(all_results: dict[str, list[dict]], round_name: str) -> str:
    """Generate markdown report for the error taxonomy."""
    lines = []
    lines.append(f"# Error Taxonomy — {round_name}\n")
    lines.append(f"> Auto-generated by `scripts/data/error_taxonomy.py`\n")

    for role_label, results in all_results.items():
        if not results:
            continue

        lines.append(f"\n## {role_label}\n")

        # Overall table
        error_counts = Counter(r["error_type"] for r in results)
        total = len(results)
        lines.append(f"Total failure attempts: **{total}**\n")

        # Group errors by category
        categories = {
            "Content Errors (Answer Wrong)": [
                "wrong_answer_calculation", "wrong_answer_math_form",
                "wrong_answer_unevaluated_expr", "wrong_answer_partial",
                "wrong_answer_rounding", "wrong_answer_off_by_2x",
                "wrong_answer_off_by_10x", "wrong_answer_zero",
                "wrong_answer_extra_text", "wrong_answer_choice_letter",
                "wrong_answer_wrong_entity", "wrong_answer_partial_overlap",
                "wrong_answer_numeric_close", "wrong_answer_numeric_far",
                "wrong_answer_over_verbose",
                "wrong_answer_code_logic", "wrong_answer_not_code",
                "wrong_answer_numeric_not_code", "wrong_answer_description_not_code",
                "wrong_answer_trivial_code", "wrong_answer_wrong_function_name",
                "wrong_tool_arguments", "wrong_tool_partial_match",
                "wrong_tool_completely",
                "wrong_answer_nl_instead_of_tool",
            ],
            "Infrastructure / Protocol Errors": [
                "no_tool_call_issued", "no_tool_call_in_answer",
                "no_finish_or_incomplete", "empty_answer",
                "loop_or_stall",
            ],
            "Information / Delegation Errors": [
                "instruction_missing_context",
                "refusal_or_hedging", "refusal_cannot_execute",
            ],
            "Verifier Issues": [
                "verify_mismatch",
            ],
        }

        lines.append("| Error Type | Count | % | Top Sources |")
        lines.append("|-----------|------:|---:|------------|")

        for cat_name, cat_errors in categories.items():
            cat_total = sum(error_counts.get(e, 0) for e in cat_errors)
            if cat_total == 0:
                continue
            lines.append(f"| **{cat_name}** | **{cat_total}** | **{cat_total*100/total:.1f}%** | |")
            for err in cat_errors:
                count = error_counts.get(err, 0)
                if count == 0:
                    continue
                # Top sources
                src_counts = Counter(r["source"] for r in results if r["error_type"] == err)
                top_src = ", ".join(f"{s}:{c}" for s, c in src_counts.most_common(3))
                lines.append(f"| `{err}` | {count} | {count*100/total:.1f}% | {top_src} |")

        # Per-source breakdown
        sources = sorted(set(r["source"] for r in results))
        for src in sources:
            src_results = [r for r in results if r["source"] == src]
            src_errors = Counter(r["error_type"] for r in src_results)
            src_total = len(src_results)
            lines.append(f"\n### {src} ({src_total} failures)\n")
            lines.append("| Error Type | Count | % |")
            lines.append("|-----------|------:|---:|")
            for err, count in src_errors.most_common():
                lines.append(f"| `{err}` | {count} | {count*100/src_total:.1f}% |")

        # Sample cases
        lines.append(f"\n### Sample Cases\n")
        for err, _ in error_counts.most_common(15):
            count = error_counts[err]
            if count == 0:
                continue
            samples = [r for r in results if r["error_type"] == err][:3]
            lines.append(f"**`{err}`** ({count} cases)")
            for s in samples:
                lines.append(f"- idx={s['idx']}, src={s['source']}, "
                           f"gold=`{s['gold'][:80]}`, pred=`{s['pred'][:80]}`")
            lines.append("")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Error taxonomy analysis")
    parser.add_argument("--round", default="round1", help="Round directory name")
    parser.add_argument("--role", default="both", choices=["router", "teacher", "both"])
    parser.add_argument("--markdown", action="store_true", help="Output markdown report")
    args = parser.parse_args()

    base = Path("data/sft") / args.round / "trajectories"

    all_results = {}

    if args.role in ("router", "both"):
        router_path = base / "router_trajectories.jsonl"
        if router_path.exists():
            router_results = analyze_router_trajectories(router_path)
            all_results["Router Probe Failures"] = router_results
            if not args.markdown:
                print_report(router_results, f"Router Probe Failures — {args.round}")

    if args.role in ("teacher", "both"):
        teacher_path = base / "trajectories.jsonl"
        if teacher_path.exists():
            teacher_results = analyze_teacher_trajectories(teacher_path)
            all_results["Teacher Trajectory Failures"] = teacher_results
            if not args.markdown:
                print_report(teacher_results, f"Teacher Trajectory Failures — {args.round}")

    if args.markdown:
        md = generate_markdown_report(all_results, args.round)
        print(md)


if __name__ == "__main__":
    main()
