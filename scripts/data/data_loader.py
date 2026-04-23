"""Data loading: fetch questions from HuggingFace datasets and extract Q/A pairs.

Each dataset has its own extraction logic in _extract_qa().
"""

from __future__ import annotations

import re

import yaml


def load_recipe(path: str) -> list[dict]:
    with open(path) as f:
        return yaml.safe_load(f)["datasets"]


def load_question_pool(recipe: list[dict]) -> list[dict]:
    from datasets import load_dataset

    all_tasks = []
    for entry in recipe:
        name = entry["name"]
        n = entry.get("n_samples")
        print(f"  Loading {name} (n={'all' if n is None else n})...", end=" ", flush=True)
        try:
            kw = {"split": entry["split"]}
            if n is not None:
                kw["streaming"] = True
            if "parquet_pattern" in entry:
                ds = load_dataset("parquet", data_files=entry["parquet_pattern"], **kw)
            elif "hf_subset" in entry:
                ds = load_dataset(entry["hf_path"], entry["hf_subset"], **kw)
            else:
                ds = load_dataset(entry["hf_path"], **kw)
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        filter_sources = set(entry.get("filter_sources", []))
        count = 0
        for row in ds:
            if n is not None and count >= n:
                break
            if filter_sources and row.get("source", "") not in filter_sources:
                continue
            q, gold = _extract_qa(name, row)
            if q and gold:
                all_tasks.append({
                    "question": q,
                    "gold_answer": gold,
                    "source": name,
                    "domain": entry["domain"],
                })
                count += 1
        print(f"{count} loaded")
    return all_tasks


def _extract_qa(name: str, row: dict) -> tuple[str, str]:
    """Extract (question, gold_answer) from a dataset row.

    Each dataset has a different schema — this function dispatches by name.
    """
    if "gsm8k" in name:
        answer = row.get("answer", "")
        m = re.search(r'####\s*(.+)', answer)
        return row.get("question", ""), m.group(1).strip() if m else ""

    if "numinamath" in name:
        problem = row.get("problem", "")
        # Skip proof questions — no numerical answer for the pipeline
        if re.search(r'\bProve\b|\bprove\b|\bShow that\b|\bshow that\b', problem):
            return "", ""
        sol = row.get("solution", "")
        m = re.findall(r'\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}', sol)
        return problem, m[-1] if m else ""

    if "dapo" in name:
        prompt = row.get("prompt", [])
        return (
            (prompt[0]["content"] if prompt else ""),
            row.get("reward_model", {}).get("ground_truth", ""),
        )

    if "drop" in name:
        spans = row.get("answers_spans", {}).get("spans", [])
        passage = row.get("passage", "")
        question = row.get("question", "")
        return f"Passage: {passage}\n\nQuestion: {question}", spans[0] if spans else ""

    if "hotpotqa" in name:
        return row.get("question", ""), row.get("answer", "")

    if "musique" in name:
        return row.get("question", ""), row.get("answer", "")

    if "taco" in name:
        sols = row.get("solutions", "")
        return row.get("question", ""), (sols[0] if isinstance(sols, list) and sols else str(sols))

    if "toolace" in name:
        convs = row.get("conversations", [])
        q = next((c.get("value", "") for c in convs if c.get("from") == "user"), "")
        gold = next((c.get("value", "") for c in convs if c.get("from") == "assistant"), "")
        # Include the system prompt with tool definitions in the question
        system = row.get("system", "")
        if system:
            q = system.strip() + "\n\n---\nUser query: " + q
        return q, gold

    return row.get("question", ""), row.get("answer", "")
