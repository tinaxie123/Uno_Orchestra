"""
Build the HuggingFace dataset release layout for Uno-Curriculum.

Output layout (under ``data/sft/hf_release/``):

    README.md              — dataset card with multi-config YAML frontmatter
    sft/train.parquet      — 2,762 teacher-verified trajectories
    rl/train.parquet       — 4,549 unresolved tasks (router + teacher both fail)

SFT schema
----------
``id``, ``source``, ``domain``, ``verifier``, ``question``, ``gold_answer``,
``final_answer``, ``strategy``, ``n_delegates``, ``n_turns``, ``subtasks``
(list<struct>), ``planner_prompt_version``, ``system_prompt``, ``messages``
(list<struct>).

RL schema
---------
``id``, ``source``, ``domain``, ``verifier``, ``question``, ``gold_answer``.

The RL pool is the set of tasks that the teacher cannot solve and whose router
probe also fails; it is derived from
``data/sft/merged/trajectories/trajectories.jsonl`` (``teacher_ok = False``).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


ROOT = Path("/data/xieht/multiagentRL/data/sft")
SFT_IN = ROOT / "merged" / "sft.parquet"
RL_IN = ROOT / "merged" / "trajectories" / "trajectories.jsonl"
OUT_DIR = ROOT / "hf_release"
SFT_DIR = OUT_DIR / "sft"
RL_DIR = OUT_DIR / "rl"

SOURCE_TO_VERIFIER = {
    "gsm8k": "math",
    "numinamath": "math",
    "drop": "qa",
    "hotpotqa": "qa",
    "musique": "qa",
    "taco": "code",
    "toolace": "toolace",
}

PROMPT_TYPE_NORMALIZE = {
    "planner_default": "v1_default",
    "planner_with_tools_toolace": "v1_with_tool_schema",
    "planner_with_tools": "v1_with_tool_schema",
}


def _decode(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    if hasattr(v, "tolist"):
        return v.tolist()
    return v


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


def _planner_prompt_version(row: dict) -> str:
    raw = str(row.get("prompt_type") or "")
    return PROMPT_TYPE_NORMALIZE.get(raw, raw or "v1_default")


def build_sft() -> list[dict]:
    df = pd.read_parquet(SFT_IN)
    print(f"SFT in: {len(df)} rows")

    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        raw_msgs = _decode(r.get("conversations_raw")) or []
        messages = [_norm_message(m) for m in raw_msgs if isinstance(m, dict)]

        system_prompt = ""
        if messages and messages[0]["from"] == "system":
            system_prompt = messages[0]["value"]
            messages = messages[1:]

        raw_delegations = _decode(r.get("delegations")) or []
        subtasks = [_norm_subtask(d) for d in raw_delegations if isinstance(d, dict)]

        source = str(r["source"])
        rows.append({
            "id": f"{source}_{i:06d}",
            "source": source,
            "domain": str(r["domain"]),
            "verifier": SOURCE_TO_VERIFIER[source],
            "question": str(r["question"]),
            "gold_answer": str(r["gold_answer"]),
            "final_answer": str(r["final_answer"]),
            "strategy": str(r["strategy"]),
            "n_delegates": int(r["n_delegates"]),
            "n_turns": len(messages) + (1 if system_prompt else 0),
            "subtasks": subtasks,
            "planner_prompt_version": _planner_prompt_version(r),
            "system_prompt": system_prompt,
            "messages": messages,
        })
    return rows


def write_sft(rows: list[dict]) -> None:
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
        pa.field("verifier", pa.string()),
        pa.field("question", pa.string()),
        pa.field("gold_answer", pa.string()),
        pa.field("final_answer", pa.string()),
        pa.field("strategy", pa.string()),
        pa.field("n_delegates", pa.int32()),
        pa.field("n_turns", pa.int32()),
        pa.field("subtasks", pa.list_(subtask_struct)),
        pa.field("planner_prompt_version", pa.string()),
        pa.field("system_prompt", pa.string()),
        pa.field("messages", pa.list_(message_struct)),
    ])
    SFT_DIR.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    out = SFT_DIR / "train.parquet"
    pq.write_table(table, out, compression="zstd")
    print(f"SFT out: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB, {len(rows)} rows)")


def build_rl() -> list[dict]:
    seen = {}
    with RL_IN.open() as f:
        for line in f:
            d = json.loads(line)
            if d.get("teacher_ok"):
                continue
            key = (d["source"], d["idx"])
            seen[key] = d
    rows = []
    for i, ((src, _), d) in enumerate(seen.items()):
        rows.append({
            "id": f"{src}_{i:06d}",
            "source": src,
            "domain": str(d["domain"]),
            "verifier": SOURCE_TO_VERIFIER[src],
            "question": str(d["question"]),
            "gold_answer": str(d["gold_answer"]),
        })
    print(f"RL: {len(rows)} tasks")
    return rows


def write_rl(rows: list[dict]) -> None:
    schema = pa.schema([
        pa.field("id", pa.string()),
        pa.field("source", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("verifier", pa.string()),
        pa.field("question", pa.string()),
        pa.field("gold_answer", pa.string()),
    ])
    RL_DIR.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    out = RL_DIR / "train.parquet"
    pq.write_table(table, out, compression="zstd")
    print(f"RL out: {out} ({out.stat().st_size / 1024 / 1024:.1f} MB, {len(rows)} rows)")


def write_readme(sft_rows: list[dict], rl_rows: list[dict]) -> None:
    sft_src = Counter(r["source"] for r in sft_rows)
    sft_dom = Counter(r["domain"] for r in sft_rows)
    sft_strat = Counter(r["strategy"] for r in sft_rows)
    rl_src = Counter(r["source"] for r in rl_rows)
    rl_dom = Counter(r["domain"] for r in rl_rows)

    def tbl(counter: Counter, header: str) -> str:
        total = sum(counter.values())
        lines = [f"| {header} | Count | Share |", "|---|---:|---:|"]
        for k, c in counter.most_common():
            lines.append(f"| {k} | {c} | {c * 100 / total:.1f}% |")
        return "\n".join(lines)

    yaml = """---
license: apache-2.0
task_categories:
- text-generation
language:
- en
tags:
- router
- multi-agent
- sft
- rl
- hierarchical-delegation
size_categories:
- 1K<n<10K
configs:
- config_name: sft
  data_files:
  - split: train
    path: sft/train.parquet
- config_name: rl
  data_files:
  - split: train
    path: rl/train.parquet
---
"""

    body = f"""# Uno Curriculum

Training curriculum for a hierarchical-delegation router: a small language
model that decomposes a task into subtasks and routes each subtask to a
(worker model, skill) pair. The curriculum is split into two configs:

- **`sft`** ({len(sft_rows):,} rows): teacher-verified trajectories used for
  imitation-learning warm-start.
- **`rl`** ({len(rl_rows):,} rows): unresolved tasks where both the router and
  the teacher fail; used as the outcome-reward pool for reinforcement
  learning.

Both configs draw from the same seven public benchmarks across five capability
axes (atomic reasoning, compositional reasoning, knowledge retrieval,
knowledge composition, tool orchestration).

## Load

```python
from datasets import load_dataset

sft = load_dataset("tinaxie/Uno-Curriculum", "sft", split="train")
rl  = load_dataset("tinaxie/Uno-Curriculum", "rl",  split="train")
```

## Config: `sft` ({len(sft_rows):,} trajectories)

Each row is a complete teacher-generated trajectory that solves the task
correctly, formatted as a ShareGPT conversation the student is trained to
imitate. The teacher is Qwen3.5-Plus; the planner prompt is source-aware
(ToolACE uses the dataset's native tool-schema injection; other sources use a
uniform planner prompt).

### Schema

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Stable identifier: `{{source}}_{{row:06d}}` |
| `source` | `string` | Benchmark source |
| `domain` | `string` | Capability axis |
| `verifier` | `string` | Which verifier scores this task: `math` / `qa` / `code` / `toolace` |
| `question` | `string` | Raw task prompt |
| `gold_answer` | `string` | Ground-truth answer |
| `final_answer` | `string` | Teacher's final answer (matches gold under the verifier) |
| `strategy` | `string` | `direct` (no delegation) / `single` / `multi` |
| `n_delegates` | `int32` | Number of subtasks issued by the planner |
| `n_turns` | `int32` | Total turns including the system turn |
| `subtasks` | `list<struct>` | Per-delegate record — fields below |
| `planner_prompt_version` | `string` | `v1_default` or `v1_with_tool_schema` |
| `system_prompt` | `string` | Full planner system prompt used for this trajectory |
| `messages` | `list<struct>` | ShareGPT turns (without the leading system turn) |

**`subtasks[]` struct**: `task_id` (str), `instruction` (str),
`model` (str, the routed worker model), `skill` (str, the routed skill),
`result` (str, the worker response observed by the planner).

**`messages[]` struct**: `from` (one of `human`, `gpt`, `function_call`,
`observation` — ShareGPT roles), `value` (str). Training loss is applied on
`gpt` and `function_call` turns. To reconstruct the full ShareGPT conversation
for training, prepend the system turn:

```python
conversation = [{{"from": "system", "value": row["system_prompt"]}}] + row["messages"]
```

### SFT distribution

**By source**
{tbl(sft_src, "Source")}

**By capability domain**
{tbl(sft_dom, "Domain")}

**By planner strategy**
{tbl(sft_strat, "Strategy")}

## Config: `rl` ({len(rl_rows):,} tasks)

Task pool for outcome-reward RL: each row is a task where both the router
(pass@3) and the teacher fail. The RL loop rolls out the router on these
tasks and uses the per-source verifier to produce a sparse reward.

### Schema

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Stable identifier |
| `source` | `string` | Benchmark source |
| `domain` | `string` | Capability axis |
| `verifier` | `string` | Verifier to use at rollout time (`math` / `qa` / `code` / `toolace`) |
| `question` | `string` | Task prompt |
| `gold_answer` | `string` | Ground-truth answer (for verifier scoring only) |

### RL distribution

**By source**
{tbl(rl_src, "Source")}

**By capability domain**
{tbl(rl_dom, "Domain")}

## Curriculum construction

Tasks pass through a three-stage filter:

1. **Router probe** (pass@3): the current policy router attempts the task
   three times. Any success discards the task from the curriculum.
2. **Teacher trajectory**: a strong teacher model solves the remaining tasks.
   Successful trajectories become **SFT demonstrations**; failed trajectories
   become the **RL pool**.
3. **Overlong filtering** (SFT only): trajectories exceeding 8,192 tokens are
   discarded to match the training context length.

See the paper / repo `docs/error taxonomy.md` for the full taxonomy of teacher
and router failures driving curriculum composition.
"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "README.md").write_text(yaml + body, encoding="utf-8")
    print(f"README: {OUT_DIR / 'README.md'}")


def main() -> None:
    sft_rows = build_sft()
    write_sft(sft_rows)
    rl_rows = build_rl()
    write_rl(rl_rows)
    write_readme(sft_rows, rl_rows)


if __name__ == "__main__":
    main()
