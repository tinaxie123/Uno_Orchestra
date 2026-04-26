"""Strip teacher-prompt leak markers from router_sft.json human turns.

The SFT corpus (router_sft.json, 61,201 records) baked up to four scaffolding
signals into every `from=human` turn that the RL prompt pool deliberately
strips before training (see scripts/rl/prepare_prompt_pool.py
:_clean_question):

    1. "Correct answer (for your reference; arrive at this through proper
       decomposition): <answer>"             -> the answer itself
    2. "REAL EVIDENCE (from the dataset ...)" + passages
       + "IMPORTANT: Your <obs> tags ..."    -> source evidence + obs guidance
    3. "BEHAVIORAL HINT: ... LAZY MODE ..."  -> termination hint
    4. "Output the trajectory now."          -> trailing template marker

Training on (1)+(2)+(3) as plain user input teaches the model to *copy* from
the prompt rather than reason. At RL time the same turn arrives with all
four stripped and the model collapses to format_error / never emits
<final_answer>. This script rewrites each human turn to the byte-shape RL
produces:

    Question: <clean_question>

    Output the trajectory now.

System / gpt / function_call / observation turns are passed through
unchanged — those are the SFT labels we still want to learn.

Default behaviour matches RL byte-for-byte (all three flags on). Toggles
exist so we can A/B isolate which leak channel matters most.

Usage
-----
    python scripts/data/strip_sft_leak.py \\
        --in  /data/xieht/LlamaFactory/data/router_sft.json \\
        --out /data/xieht/LlamaFactory/data/router_sft_clean.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


# Marker boundaries — must stay in sync with
# scripts/rl/prepare_prompt_pool.py:_clean_question. Each value is the
# anchor we look for in the human turn; the turn is truncated at the
# earliest occurrence across all enabled groups.
MARKER_GROUPS: dict[str, tuple[str, ...]] = {
    "answer":   ("\n\nCorrect answer", "\n\nThe correct answer"),
    "evidence": ("\n\nREAL EVIDENCE",),
    "hint":     ("\n\nBEHAVIORAL HINT",),
    # The "Output the trajectory" marker is always present at the tail of
    # leaked records; we always cut it (and re-add a canonical copy).
    "trail":    ("\n\nOutput the trajectory",),
}


def clean_human(
    value: str,
    *,
    strip_answer: bool,
    strip_evidence: bool,
    strip_hint: bool,
) -> tuple[str, set[str]]:
    """Return (rewritten human turn, set of marker groups that fired)."""
    text = value.strip()
    if text.startswith("Question:"):
        text = text[len("Question:"):].lstrip()

    enabled = ["trail"]  # always cut trailing template marker
    if strip_answer:   enabled.append("answer")
    if strip_evidence: enabled.append("evidence")
    if strip_hint:     enabled.append("hint")

    cuts: list[int] = []
    hit: set[str] = set()
    for grp in enabled:
        for marker in MARKER_GROUPS[grp]:
            idx = text.find(marker)
            if idx != -1:
                cuts.append(idx)
                hit.add(grp)

    if cuts:
        text = text[: min(cuts)]
    text = text.strip()
    return f"Question: {text}\n\nOutput the trajectory now.", hit


def process_record(
    rec: dict,
    *,
    strip_answer: bool,
    strip_evidence: bool,
    strip_hint: bool,
    tally: Counter,
) -> bool:
    """Mutate `rec` in place; return True iff any human turn changed."""
    convs = rec.get("conversations") or []
    changed = False
    for c in convs:
        if c.get("from") != "human":
            continue
        old = c.get("value", "")
        new, hit = clean_human(
            old,
            strip_answer=strip_answer,
            strip_evidence=strip_evidence,
            strip_hint=strip_hint,
        )
        if new != old:
            c["value"] = new
            changed = True
            for h in hit:
                tally[f"strip_{h}"] += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True,
                    help="Path to router_sft.json (input).")
    ap.add_argument("--out", required=True,
                    help="Path to router_sft_clean.json (output).")
    ap.add_argument("--stats", default=None,
                    help="Stats JSON path (default: <out>.stats.json).")
    ap.add_argument("--strip-answer", dest="strip_answer",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Strip 'Correct answer (for your reference ...)' line.")
    ap.add_argument("--strip-evidence", dest="strip_evidence",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Strip 'REAL EVIDENCE ...' block + IMPORTANT/Paraphrase.")
    ap.add_argument("--strip-hint", dest="strip_hint",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="Strip 'BEHAVIORAL HINT ...' line.")
    ap.add_argument("--overwrite", action="store_true",
                    help="Allow overwriting an existing --out file.")
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    if out.exists() and not args.overwrite:
        print(f"refusing to overwrite existing {out} (pass --overwrite)",
              file=sys.stderr)
        return 2

    print(f"[load] {inp} ({inp.stat().st_size / 1e6:.1f} MB)")
    with inp.open() as f:
        records = json.load(f)
    print(f"  records: {len(records)}")
    print(f"[flags] strip_answer={args.strip_answer}  "
          f"strip_evidence={args.strip_evidence}  "
          f"strip_hint={args.strip_hint}")

    tally: Counter[str] = Counter()
    n_changed = 0
    for rec in records:
        if process_record(
            rec,
            strip_answer=args.strip_answer,
            strip_evidence=args.strip_evidence,
            strip_hint=args.strip_hint,
            tally=tally,
        ):
            n_changed += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[write] {out}")
    with out.open("w") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"  wrote {len(records)} records ({out.stat().st_size / 1e6:.1f} MB), "
          f"modified {n_changed}")

    stats_path = Path(args.stats) if args.stats else out.with_suffix(".stats.json")
    stats = {
        "input": str(inp),
        "output": str(out),
        "n_records": len(records),
        "n_records_modified": n_changed,
        "flags": {
            "strip_answer": args.strip_answer,
            "strip_evidence": args.strip_evidence,
            "strip_hint": args.strip_hint,
        },
        "marker_hits": dict(tally),
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"[stats] {stats_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
