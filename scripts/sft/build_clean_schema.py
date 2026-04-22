"""
Transform the merged SFT parquet into a clean, HF-viewer-friendly schema.

Input
-----
data/sft/merged/sft.parquet                — the dedup-merged training set
                                             (see ``scripts/sft/merge_rounds.py``)

Output
------
data/sft/merged/sft_clean.parquet          — the public / paper-ready artifact
data/sft/merged/README.md                  — dataset card (schema + stats)

Motivation
----------
The internal parquet stores ``conversations_raw``, ``delegations``,
``models_used`` and ``skills_used`` as JSON-encoded strings. That is fine for
training but makes the HuggingFace dataset viewer (and downstream analysis)
useless — every column renders as an opaque blob.

This script produces a schema where:
1. ``messages`` is a native ``list<struct>`` of ``{from, value}`` (not a JSON
   string), so the viewer can expand turns row-by-row.
2. ``subtasks`` is a native ``list<struct>`` with per-delegation fields
   (``task_id``, ``instruction``, ``model``, ``skill``, ``result``).
3. ``models_used`` and ``skills_used`` are native ``list<str>``.
4. ``system_prompt`` is split out of the first message into its own column so
   readers can see which planner prompt was used.
5. A stable ``id`` is added (``{source}_{row_index:06d}``) so trajectories can
   be referenced from the paper or linked external analyses.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


ROOT = Path("/data/xieht/multiagentRL/data/sft")
IN_PATH = ROOT / "merged" / "sft.parquet"
OUT_PATH = ROOT / "merged" / "sft_clean.parquet"
README_PATH = ROOT / "merged" / "README.md"


def _decode_str_or_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _norm_subtask(raw: dict) -> dict:
    return {
        "task_id": str(raw.get("task_id") or ""),
        "instruction": str(raw.get("instruction") or ""),
        "model": str(raw.get("routed_model") or raw.get("model") or ""),
        "skill": str(raw.get("routed_skill") or raw.get("skill") or ""),
        "result": str(raw.get("worker_response") or raw.get("result") or ""),
    }


def _norm_message(raw: dict) -> dict:
    return {
        "from": str(raw.get("from") or raw.get("role") or ""),
        "value": str(raw.get("value") or raw.get("content") or ""),
    }


def convert() -> None:
    df = pd.read_parquet(IN_PATH)
    print(f"loaded {len(df)} rows from {IN_PATH}")

    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        raw_msgs = _decode_str_or_list(r.get("conversations_raw")) or []
        messages = [_norm_message(m) for m in raw_msgs if isinstance(m, dict)]

        # Split out the leading system turn as system_prompt.
        system_prompt = ""
        if messages and messages[0]["from"] == "system":
            system_prompt = messages[0]["value"]
            messages = messages[1:]

        raw_delegations = _decode_str_or_list(r.get("delegations")) or []
        subtasks = [_norm_subtask(d) for d in raw_delegations if isinstance(d, dict)]

        models_used = _decode_str_or_list(r.get("models_used")) or []
        skills_used = _decode_str_or_list(r.get("skills_used")) or []

        rows.append({
            "id": f"{r['source']}_{i:06d}",
            "source": str(r.get("source") or ""),
            "domain": str(r.get("domain") or ""),
            "prompt_type": str(r.get("prompt_type") or ""),
            "question": str(r.get("question") or ""),
            "gold_answer": str(r.get("gold_answer") or ""),
            "final_answer": str(r.get("final_answer") or ""),
            "strategy": str(r.get("strategy") or ""),
            "n_delegates": int(r.get("n_delegates") or 0),
            "subtasks": subtasks,
            "models_used": [str(m) for m in models_used],
            "skills_used": [str(s) for s in skills_used],
            "system_prompt": system_prompt,
            "messages": messages,
        })

    # Build an explicit Arrow schema so list<struct> columns have named fields
    # in the parquet metadata (this is what HF viewer reads).
    message_struct = pa.struct([pa.field("from", pa.string()),
                                pa.field("value", pa.string())])
    subtask_struct = pa.struct([pa.field("task_id", pa.string()),
                                pa.field("instruction", pa.string()),
                                pa.field("model", pa.string()),
                                pa.field("skill", pa.string()),
                                pa.field("result", pa.string())])
    schema = pa.schema([
        pa.field("id", pa.string()),
        pa.field("source", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("prompt_type", pa.string()),
        pa.field("question", pa.string()),
        pa.field("gold_answer", pa.string()),
        pa.field("final_answer", pa.string()),
        pa.field("strategy", pa.string()),
        pa.field("n_delegates", pa.int32()),
        pa.field("subtasks", pa.list_(subtask_struct)),
        pa.field("models_used", pa.list_(pa.string())),
        pa.field("skills_used", pa.list_(pa.string())),
        pa.field("system_prompt", pa.string()),
        pa.field("messages", pa.list_(message_struct)),
    ])

    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, OUT_PATH, compression="zstd")
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB)")

    write_readme(rows)


def write_readme(rows: list[dict]) -> None:
    src_counts = Counter(r["source"] for r in rows)
    dom_counts = Counter(r["domain"] for r in rows)
    strat_counts = Counter(r["strategy"] for r in rows)
    avg_turns = sum(len(r["messages"]) for r in rows) / len(rows)
    avg_delegates = sum(r["n_delegates"] for r in rows) / len(rows)

    def table_from_counter(counter: Counter, header: str) -> str:
        total = sum(counter.values())
        lines = [f"| {header} | Count | Share |", "|---|---:|---:|"]
        for k, c in counter.most_common():
            lines.append(f"| {k} | {c} | {c * 100 / total:.1f}% |")
        return "\n".join(lines)

    readme = f"""# Router SFT — Merged Curriculum

Supervised-fine-tuning trajectories for the hierarchical-delegation router
described in our paper. Each row is a single teacher-verified trajectory over
a task drawn from one of seven public benchmarks.

- **Rows**: {len(rows):,}
- **Format**: ShareGPT (native `list<struct>` columns; no JSON-string blobs)
- **Teacher**: Qwen3.5-Plus (trajectories are only included when the teacher's
  final answer matches the gold label under the per-source verifier)
- **Planner prompt**: source-aware — ToolACE uses the dataset's native
  tool-schema injection; all other sources use a uniform planner prompt

## Schema

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Stable identifier: `{{source}}_{{row:06d}}` |
| `source` | `string` | Benchmark source (gsm8k, numinamath, drop, hotpotqa, musique, taco, toolace) |
| `domain` | `string` | Capability axis (atomic_reasoning, compositional_reasoning, knowledge_retrieval, knowledge_composition, tool_orchestration) |
| `prompt_type` | `string` | Planner-prompt variant (`planner_default` / `planner_with_tools_toolace`) |
| `question` | `string` | Raw task prompt |
| `gold_answer` | `string` | Ground-truth answer used by the verifier |
| `final_answer` | `string` | Teacher's final answer after the full trajectory |
| `strategy` | `string` | `direct` (no delegate), `single` (one delegate), or `multi` |
| `n_delegates` | `int32` | Number of subtasks the planner issued |
| `subtasks` | `list<struct>` | Per-delegate record — see below |
| `models_used` | `list<string>` | Deduped list of router-selected worker models |
| `skills_used` | `list<string>` | Deduped list of router-selected skills |
| `system_prompt` | `string` | Planner system prompt (split out so readers can identify the prompt variant) |
| `messages` | `list<struct>` | ShareGPT turns: `{{from, value}}` — training target |

### `subtasks` struct

| Field | Type | Description |
|---|---|---|
| `task_id` | `string` | Planner-assigned subtask id (e.g. `t1`) |
| `instruction` | `string` | Self-contained instruction sent to the worker |
| `model` | `string` | Routed worker model |
| `skill` | `string` | Routed skill |
| `result` | `string` | Worker response (observation) |

### `messages` struct

| Field | Type | Description |
|---|---|---|
| `from` | `string` | One of `human`, `gpt`, `function_call`, `observation` (ShareGPT roles; training loss is on `gpt` + `function_call` turns) |
| `value` | `string` | Turn content |

Note: the original system turn has been extracted into the top-level
`system_prompt` column; the `messages` list starts from the first `human`
turn. Consumers who need the full ShareGPT conversation can reconstruct it
with `[{{"from": "system", "value": system_prompt}}] + messages`.

## Distribution

### By source
{table_from_counter(src_counts, "Source")}

### By capability domain
{table_from_counter(dom_counts, "Domain")}

### By planner strategy
{table_from_counter(strat_counts, "Strategy")}

### Aggregate statistics

| Metric | Value |
|---|---|
| Average turns per trajectory | {avg_turns:.1f} |
| Average delegates per trajectory | {avg_delegates:.2f} |

## Usage

```python
from datasets import load_dataset
ds = load_dataset("parquet", data_files="sft_clean.parquet", split="train")
row = ds[0]
# ShareGPT conversation with the system turn prepended:
conversation = [{{"from": "system", "value": row["system_prompt"]}}] + row["messages"]
```

## Curriculum provenance

Trajectories are produced by the curriculum filter documented in
`docs/error taxonomy.md`: (1) router probe discards already-solved tasks,
(2) teacher solves the remainder, (3) overlong (>8k tokens) trajectories are
filtered out. Only teacher-successful, length-bounded trajectories appear in
this file.
"""
    README_PATH.write_text(readme, encoding="utf-8")
    print(f"wrote {README_PATH}")


if __name__ == "__main__":
    convert()
