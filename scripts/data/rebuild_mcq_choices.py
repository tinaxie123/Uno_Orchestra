"""Rebuild MCQ rows in data/rl/{train,val}.parquet so the model can actually
answer them.

Problem (task #14): the upstream SFT prep dropped the answer choices for
10 MCQ source datasets. The parquet ships only the question stem +
either a letter gold ('A'/'B'/...) or — for 5 sources — the text of the
correct option. Without choices in the prompt the model cannot pick the
right letter; without a verifier that handles letters the text-gold
sources fail verify_qa's article-strip / substring fuzz.

Fix: for every row whose `source` is in the MCQ set,
    1. look up the choices from the original HuggingFace dataset (matched
       by normalised question text),
    2. embed the choices into the user prompt as
           Choices:
           (A) ...
           (B) ...
           ...
       and instruct the model to output a single letter,
    3. canonicalise gold to a single letter (text-gold sources flip via
       `text → matching_label`; letter-gold sources just unwrap '(D)' →
       'D'),
    4. mark a `mcq_letter` flag in extra_info so the verifier router can
       use a strict letter-match verifier (verify_mcq) instead of the
       fuzzy verify_qa.

Rows where HF lookup misses (rare, ~few %) keep their original
prompt/gold and are tagged with `mcq_unmatched=True` so eval can exclude
them; they continue to score 0 (same as before this rebuild — strictly
better, no regression).

Backups of the originals live next to the parquets as
`.bak_pre_mcq_rebuild`. Re-run is idempotent: rows already rewritten
(detected via the `Choices:` substring in the user prompt) are
skipped.

Usage
-----
    python scripts/data/rebuild_mcq_choices.py \
        --train data/rl/train.parquet \
        --val   data/rl/val.parquet
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from datasets import load_dataset
from huggingface_hub import HfFileSystem


# ── Source spec ─────────────────────────────────────────────────────
# Each entry tells us how to load the HF dataset and pull
# (question_text, [(label, text), ...], gold_letter) out of one row.

# bbh_logical_deduction is special: choices are already embedded in the
# `input` field of the HF row. We don't rewrite the prompt for it (it's
# already complete); we only canonicalise '(D)' → 'D' on the gold.

LETTER_GOLD_SOURCES = {
    "arc_challenge", "commonsenseqa", "openbookqa", "aqua_rat",
    "bbh_logical_deduction",
}
TEXT_GOLD_SOURCES = {
    "mmlu_aux_stem", "piqa", "social_iqa", "winogrande", "logiqa2",
}
ALL_MCQ_SOURCES = LETTER_GOLD_SOURCES | TEXT_GOLD_SOURCES


def _norm(q: str) -> str:
    """Question canonicalisation for HF lookup (matches _normalise_q in
    prepare_prompt_pool.py: lowercase, collapse whitespace, truncate)."""
    q = q.strip().lower()
    q = re.sub(r"\s+", " ", q)
    return q[:512]


# ── Per-dataset extractors ──────────────────────────────────────────
# Each returns list of (question_norm, choices_list, gold_letter) tuples.
# choices_list is [("A","text"), ("B","text"), ...] preserving HF order.

def _hf_parquet_shards(repo: str, pattern: str) -> list[str]:
    fs = HfFileSystem()
    return [f"hf://{p}" for p in fs.glob(f"datasets/{repo}/{pattern}")]


def _load_arc_challenge() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation", "test"):
        ds = load_dataset("allenai/ai2_arc", "ARC-Challenge", split=split)
        for r in ds:
            ch = r["choices"]
            choices = list(zip(ch["label"], ch["text"]))
            rows.append((_norm(r["question"]), choices, r["answerKey"].strip()))
    return rows


def _load_commonsenseqa() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation"):
        ds = load_dataset("tau/commonsense_qa", split=split)
        for r in ds:
            ch = r["choices"]
            choices = list(zip(ch["label"], ch["text"]))
            gold = (r.get("answerKey") or "").strip()
            if not gold:
                continue  # test split has no gold
            rows.append((_norm(r["question"]), choices, gold))
    return rows


def _load_openbookqa() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation", "test"):
        ds = load_dataset("allenai/openbookqa", "main", split=split)
        for r in ds:
            ch = r["choices"]
            choices = list(zip(ch["label"], ch["text"]))
            rows.append((_norm(r["question_stem"]), choices, r["answerKey"].strip()))
    return rows


def _load_aqua_rat() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation", "test"):
        ds = load_dataset("deepmind/aqua_rat", "raw", split=split)
        for r in ds:
            # options: ['A)21', 'B)21.5', ...] — strip "X)" prefix
            choices = []
            for opt in r["options"]:
                m = re.match(r"^\s*([A-E])\s*[\)\.\:]\s*(.*)$", opt)
                if m:
                    choices.append((m.group(1), m.group(2).strip()))
            if len(choices) < 2:
                continue
            rows.append((_norm(r["question"]), choices, r["correct"].strip()))
    return rows


def _load_mmlu_aux() -> list[tuple[str, list, str]]:
    rows = []
    ds = load_dataset("cais/mmlu", "all", split="auxiliary_train")
    LABELS = ["A", "B", "C", "D"]
    for r in ds:
        ch = r["choices"]
        if len(ch) != 4:
            continue
        choices = list(zip(LABELS, ch))
        rows.append((_norm(r["question"]), choices, LABELS[int(r["answer"])]))
    return rows


def _load_piqa() -> list[tuple[str, list, str]]:
    rows = []
    # parquet-direct (script-free)
    for shard in _hf_parquet_shards("lighteval/piqa", "**/*.parquet"):
        df = pd.read_parquet(shard)
        for _, r in df.iterrows():
            label = int(r["label"])
            if label not in (0, 1):  # test split has -1
                continue
            choices = [("A", str(r["sol1"])), ("B", str(r["sol2"]))]
            gold_letter = "A" if label == 0 else "B"
            rows.append((_norm(str(r["goal"])), choices, gold_letter))
    return rows


def _load_social_iqa() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation"):
        ds = load_dataset("lighteval/siqa", split=split)
        for r in ds:
            choices = [("A", r["answerA"]), ("B", r["answerB"]), ("C", r["answerC"])]
            label = int(r["label"])  # '1'/'2'/'3'
            if label not in (1, 2, 3):
                continue
            gold_letter = ["A", "B", "C"][label - 1]
            # SFT prep concatenated `context + " " + question` — that's
            # the form we'll see in our parquet. Index both forms so we
            # also catch any prep-script variant.
            ctx = r.get("context", "") or ""
            q = r.get("question", "") or ""
            combined = f"{ctx.strip()} {q.strip()}".strip()
            rows.append((_norm(combined), choices, gold_letter))
            rows.append((_norm(q), choices, gold_letter))
    return rows


def _load_winogrande() -> list[tuple[str, list, str]]:
    rows = []
    for split in ("train", "validation"):
        ds = load_dataset("allenai/winogrande", "winogrande_xl",
                          split=split, trust_remote_code=False)
        for r in ds:
            ans = (r.get("answer") or "").strip()
            if ans not in ("1", "2"):
                continue
            choices = [("A", r["option1"]), ("B", r["option2"])]
            gold_letter = "A" if ans == "1" else "B"
            rows.append((_norm(r["sentence"]), choices, gold_letter))
    return rows


def _load_logiqa2() -> list[tuple[str, list, str]]:
    rows = []
    for shard in _hf_parquet_shards("jeggers/logiqa2_formatted", "**/*.parquet"):
        df = pd.read_parquet(shard)
        for _, r in df.iterrows():
            opts = list(r["options"])
            if len(opts) != 4:
                continue
            choices = list(zip(["A", "B", "C", "D"], [str(o) for o in opts]))
            gold = str(r["answer_char"]).strip()
            if gold not in ("A", "B", "C", "D"):
                continue
            # SFT prep stored just the passage `text` for this dataset
            # (the question stem and choices were dropped). Index by
            # `text` alone first; also add `text + question` and
            # `question` alone as fallbacks so any prep variant matches.
            text = str(r["text"]).strip()
            q = str(r["question"]).strip()
            rows.append((_norm(text), choices, gold))
            rows.append((_norm(f"{text} {q}"), choices, gold))
            rows.append((_norm(q), choices, gold))
    return rows


# bbh_logical_deduction is unique: choices are already in `input`. We
# don't need to look it up — the parquet prompt already shows them. We
# only canonicalise the gold from '(D)' to 'D' below.


LOADERS = {
    "arc_challenge": _load_arc_challenge,
    "commonsenseqa": _load_commonsenseqa,
    "openbookqa": _load_openbookqa,
    "aqua_rat": _load_aqua_rat,
    "mmlu_aux_stem": _load_mmlu_aux,
    "piqa": _load_piqa,
    "social_iqa": _load_social_iqa,
    "winogrande": _load_winogrande,
    "logiqa2": _load_logiqa2,
}


def _build_index(source: str) -> dict[str, tuple[list, str]]:
    """norm_question → (choices_list, gold_letter)."""
    print(f"[mcq-rebuild] loading HF for {source} ...")
    rows = LOADERS[source]()
    idx: dict[str, tuple[list, str]] = {}
    for nq, ch, gl in rows:
        if not nq:
            continue
        # On collision, keep the first (HF train usually comes before val/test).
        if nq not in idx:
            idx[nq] = (ch, gl)
    print(f"[mcq-rebuild]   {source}: {len(idx)} unique questions indexed")
    return idx


def _format_choices(choices: list[tuple[str, str]]) -> str:
    return "\n".join(f"({lbl}) {txt}" for lbl, txt in choices)


# ── Prompt rewriting ───────────────────────────────────────────────
_OUTPUT_TAIL = "\n\nOutput the trajectory now."


def _rewrite_prompt(orig_user_content: str, choices_block: str) -> str:
    """Insert a Choices block + letter instruction before the trailing
    'Output the trajectory now.' sentence. If the marker isn't present,
    append cleanly."""
    body = orig_user_content
    tail = ""
    idx = body.rfind(_OUTPUT_TAIL.strip())
    if idx != -1:
        tail = _OUTPUT_TAIL
        body = body[:idx].rstrip()
    return (
        f"{body}\n\n"
        f"Choices:\n{choices_block}\n\n"
        f"Output a single letter (A/B/C/...) inside <final_answer>...</final_answer>."
        f"{tail}"
    )


# ── Main ──────────────────────────────────────────────────────────
def rebuild_one(parquet_path: Path, indices: dict[str, dict]) -> None:
    print(f"\n[mcq-rebuild] processing {parquet_path}")
    df = pd.read_parquet(parquet_path)
    print(f"[mcq-rebuild]   loaded {len(df)} rows")

    stats = defaultdict(lambda: {"matched": 0, "unmatched": 0, "skipped_already_done": 0})

    new_prompts = list(df["prompt"])
    new_extra = list(df["extra_info"])
    new_reward = list(df["reward_model"])
    new_env = list(df["env_kwargs"])

    for i in range(len(df)):
        ext = new_extra[i]
        src = ext.get("source", "")
        if src not in ALL_MCQ_SOURCES:
            continue

        # Special-case bbh_logical_deduction: choices already in prompt
        # via SFT's `input` field. Just canonicalise gold and tag.
        if src == "bbh_logical_deduction":
            gold = str(ext.get("gold", "")).strip()
            m = re.match(r"^\(?([A-G])\)?$", gold)
            if not m:
                stats[src]["unmatched"] += 1
                continue
            letter = m.group(1)
            ext = {**ext, "gold": letter, "mcq_letter": True, "mcq_unmatched": False}
            new_extra[i] = ext
            new_reward[i] = {"ground_truth": letter}
            env = dict(new_env[i])
            env["ground_truth"] = letter
            new_env[i] = env
            stats[src]["matched"] += 1
            continue

        # Skip if already rewritten (idempotent re-run)
        prompt = list(new_prompts[i])
        user_msg = next((m for m in prompt if m.get("role") == "user"), None)
        if user_msg and "Choices:" in user_msg.get("content", ""):
            stats[src]["skipped_already_done"] += 1
            continue

        nq = _norm(ext.get("question", ""))
        hit = indices[src].get(nq)
        if hit is None:
            ext = {**ext, "mcq_unmatched": True}
            new_extra[i] = ext
            stats[src]["unmatched"] += 1
            continue

        choices, gold_letter = hit
        choices_block = _format_choices(choices)

        # Rewrite user-turn prompt
        new_prompt = []
        for m in prompt:
            if m.get("role") == "user":
                m = dict(m)
                m["content"] = _rewrite_prompt(m["content"], choices_block)
            new_prompt.append(m)
        new_prompts[i] = new_prompt

        # Canonicalise gold to letter
        ext = {**ext, "gold": gold_letter, "mcq_letter": True, "mcq_unmatched": False}
        new_extra[i] = ext
        new_reward[i] = {"ground_truth": gold_letter}
        env = dict(new_env[i])
        env["ground_truth"] = gold_letter
        new_env[i] = env
        stats[src]["matched"] += 1

    print(f"\n[mcq-rebuild]   per-source coverage:")
    for src in sorted(stats):
        s = stats[src]
        total = s["matched"] + s["unmatched"] + s["skipped_already_done"]
        if total == 0:
            continue
        pct = 100 * s["matched"] / max(1, s["matched"] + s["unmatched"])
        print(f"     {src:<28} matched={s['matched']:>5}  unmatched={s['unmatched']:>5}  "
              f"skipped_already={s['skipped_already_done']:>4}  ({pct:.1f}% recall)")

    df = df.assign(
        prompt=new_prompts,
        extra_info=new_extra,
        reward_model=new_reward,
        env_kwargs=new_env,
    )
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, parquet_path)
    print(f"[mcq-rebuild] wrote {parquet_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--train", default="data/rl/train.parquet")
    p.add_argument("--val", default="data/rl/val.parquet")
    args = p.parse_args()

    # Build HF indices once (shared across train+val)
    indices: dict[str, dict] = {}
    for src in sorted(LOADERS):
        try:
            indices[src] = _build_index(src)
        except Exception as e:
            print(f"[mcq-rebuild] FAIL load {src}: {e!r}")
            indices[src] = {}

    rebuild_one(Path(args.train), indices)
    rebuild_one(Path(args.val), indices)
    print("\n[mcq-rebuild] done.")


if __name__ == "__main__":
    main()
