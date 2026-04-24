"""Build the RL prompt pool parquet for SkillRouter training.

Input schema (tinaxie/SkillRouter-Curriculum `sft_full`):
    id, source, category, question, gold_answer, teacher,
    distillation_pass, n_plan_rounds, n_subtasks, conversations

Output row (verl / Router-R1 convention):
    prompt         — JSON [{"role":"system", ...}, {"role":"user", ...}]
    data_source    — category (for per-source reward routing)
    ability        — "routing"
    reward_model   — JSON {"ground_truth": str}
    extra_info     — JSON {"question", "gold", "source", "tests"?}
    env_kwargs     — JSON {"question", "ground_truth", "data_source",
                           "source", "tests"?}

For TACO / codeforces_cots / codecontests, we additionally enrich each
row with `tests={"inputs":[...], "outputs":[...]}` pulled from the
original HuggingFace dataset's `input_output` field — this is the
stdin/stdout test-case oracle that verl's PRIME-code harness needs.
Without this, the env's `code_verifier` falls through to False and the
reward is always 0 on code tasks (one of the documented RL stall causes
— see README §🥦 Error Taxonomy).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


# ── HF test-case lookup for code sources ────────────────────────────
# Build `question → {"inputs": [...], "outputs": [...]}` once; every
# TACO/codeforces/codecontests row then enriches its env_kwargs from it.
_CODE_SOURCES = {"taco", "codeforces_cots", "codecontests"}


def _clean_question(q: str) -> str:
    """Strip teacher-prompt decorations from the `question` column.

    `sft_full/train.parquet` stores the full user-turn text that was fed
    to the teacher during distillation — which for many sources leaks the
    gold answer and/or per-row evidence (multi-hop passages, GSM8K step
    solutions, etc.) into the prompt. If we forwarded that to RL, the
    router would solve every task by reading the cheat sheet.

    Strip every marker the distillation template added, keeping only the
    true question text.
    """
    q = q.strip()
    if q.startswith("Question:"):
        q = q[len("Question:"):].strip()
    for marker in (
        "\n\nCorrect answer",
        "\n\nBEHAVIORAL HINT",
        "\n\nREAL EVIDENCE",
        "\n\nOutput the trajectory",
        "\n\nThe correct answer",
    ):
        idx = q.find(marker)
        if idx != -1:
            q = q[:idx].strip()
    return q


def _normalise_q(q: str) -> str:
    """Canonicalise question text for HF test-index matching. Strip
    whitespace, lower-case, collapse runs of whitespace.
    """
    q = q.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q[:512]


def _parse_input_output(raw: Any) -> dict | None:
    """Parse the `input_output` column from TACO / codeforces_cots.

    TACO stores this as a JSON *string* with {"inputs": [...], "outputs": [...]}.
    Some splits may already be a dict, or may carry fn_name (call-based,
    which PRIME doesn't handle — we drop those).
    """
    if raw is None or raw == "" or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except Exception:
            return None
    elif isinstance(raw, dict):
        obj = raw
    else:
        return None
    if not isinstance(obj, dict):
        return None
    if "fn_name" in obj:
        return None  # call-based — out of scope for PRIME
    ins, outs = obj.get("inputs"), obj.get("outputs")
    if not (isinstance(ins, list) and isinstance(outs, list) and ins and outs):
        return None
    return {"inputs": [str(x) for x in ins], "outputs": [str(x) for x in outs]}


def _build_tests_index() -> dict[str, dict]:
    """Load BAAI/TACO (train+test) and codeforces_cots, return
    question → tests mapping. Uses HF mirror via HF_ENDPOINT.
    """
    from datasets import load_dataset

    index: dict[str, dict] = {}

    # --- TACO ---
    try:
        print("[rl-pool] loading BAAI/TACO ...")
        ds = load_dataset("BAAI/TACO", split="train", trust_remote_code=True)
        n_with_tests = 0
        for row in ds:
            tests = _parse_input_output(row.get("input_output"))
            if tests is None:
                continue
            key = _normalise_q(row.get("question", ""))
            if not key:
                continue
            index[key] = tests
            n_with_tests += 1
        print(f"[rl-pool] TACO: {n_with_tests} rows with usable stdin/stdout tests")
    except Exception as e:
        print(f"[rl-pool] WARN: TACO load failed ({e!r}); code rewards will be 0")

    # --- codeforces_cots (open-r1/codeforces-cots) ---
    try:
        print("[rl-pool] loading open-r1/codeforces-cots ...")
        ds = load_dataset("open-r1/codeforces-cots", split="train", trust_remote_code=True)
        n_with_tests = 0
        for row in ds:
            tests = _parse_input_output(row.get("input_output"))
            if tests is None:
                # try the "examples" field Codeforces sometimes ships
                ex = row.get("examples")
                if isinstance(ex, list) and ex:
                    tests = {
                        "inputs": [str(e.get("input", "")) for e in ex],
                        "outputs": [str(e.get("output", "")) for e in ex],
                    }
            if tests is None:
                continue
            key = _normalise_q(row.get("description", "") or row.get("question", ""))
            if not key:
                continue
            index[key] = tests
            n_with_tests += 1
        print(f"[rl-pool] codeforces_cots: {n_with_tests} rows with tests")
    except Exception as e:
        print(f"[rl-pool] WARN: codeforces_cots load failed ({e!r})")

    print(f"[rl-pool] total test-index size: {len(index)}")
    return index


# ── Main transform ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True,
                        help="Path to sft_full/train.parquet (new schema)")
    parser.add_argument("--system-prompt", required=True,
                        help="Path to system_prompt.txt (schema instructions)")
    parser.add_argument("--output-train", required=True)
    parser.add_argument("--output-val", required=True)
    parser.add_argument("--val-ratio", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-code-tests", action="store_true",
                        help="Skip HF TACO/codeforces load (for smoke tests). "
                             "env_kwargs will have no `tests` field — code "
                             "tasks will get reward 0. Don't use for real training.")
    parser.add_argument("--filter-sources", default="",
                        help="Comma-separated list of sources to keep "
                             "(default: all). Example: taco,musique_answerable,toolace")
    args = parser.parse_args()

    df = pd.read_parquet(args.input)
    print(f"[rl-pool] loaded {len(df)} rows from {args.input}")

    if args.filter_sources:
        wanted = {s.strip() for s in args.filter_sources.split(",") if s.strip()}
        df = df[df["source"].astype(str).isin(wanted)].reset_index(drop=True)
        print(f"[rl-pool] filtered to sources {sorted(wanted)}: {len(df)} rows")

    system_prompt = Path(args.system_prompt).read_text()

    # Build the HF test-case index only if we have any code rows
    has_code = bool(df["source"].astype(str).str.lower().isin(_CODE_SOURCES).any())
    tests_index: dict[str, dict] = {}
    if has_code and not args.skip_code_tests:
        tests_index = _build_tests_index()
    elif has_code and args.skip_code_tests:
        print("[rl-pool] --skip-code-tests set; code rewards will be 0")

    rows = []
    skipped = 0
    code_with_tests = code_without_tests = 0
    for idx in range(len(df)):
        row = df.iloc[idx]
        source = str(row.get("source", "unknown"))
        category = str(row.get("category", "unknown"))
        question = _clean_question(str(row.get("question", "")))
        gold = str(row.get("gold_answer", "")).strip()

        if not question or not gold:
            skipped += 1
            continue

        tests = None
        if source.lower() in _CODE_SOURCES and tests_index:
            tests = tests_index.get(_normalise_q(question))
            if tests:
                code_with_tests += 1
            else:
                code_without_tests += 1

        env_kwargs = {
            "question": question,
            "ground_truth": gold,
            "data_source": category,
            "source": source,
        }
        if tests is not None:
            env_kwargs["tests"] = tests

        extra_info = {
            "question": question,
            "gold": gold,
            "source": source,
            "category": category,
        }
        if tests is not None:
            extra_info["tests"] = tests

        rows.append({
            "prompt": json.dumps([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Question: {question}\n\nOutput the trajectory now."},
            ]),
            "data_source": category,
            "ability": "routing",
            "reward_model": json.dumps({"ground_truth": gold}),
            "extra_info": json.dumps(extra_info),
            "env_kwargs": json.dumps(env_kwargs),
        })

    print(f"[rl-pool] packed {len(rows)} rows, skipped {skipped}")
    if has_code:
        print(f"[rl-pool]   code rows with tests:    {code_with_tests}")
        print(f"[rl-pool]   code rows WITHOUT tests: {code_without_tests} (will score 0)")

    import random
    random.seed(args.seed)
    random.shuffle(rows)
    val_size = int(len(rows) * args.val_ratio)
    val_rows, train_rows = rows[:val_size], rows[val_size:]

    for path, data, name in [
        (args.output_train, train_rows, "train"),
        (args.output_val, val_rows, "val"),
    ]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        table = pa.table({k: [r[k] for r in data] for k in data[0].keys()})
        pq.write_table(table, path)
        print(f"[rl-pool] wrote {len(data)} {name} → {path}")


if __name__ == "__main__":
    main()
