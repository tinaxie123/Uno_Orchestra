"""
Convert a train_{snapshot}.parquet (sft_protocol v1) into a LLaMA-Factory
ingestible parquet + dataset_info.json snippet.

LLaMA-Factory ShareGPT format:
- Row column: `conversations` (list of dicts)
- Per-message keys: `from` and `value`
- Role tag values: human / gpt / system / observation / function_call
- Optional `system` column for a shared system prompt (avoids duplicating
  the same system prompt across every row)

Input  : data/sft/train_{snapshot}.parquet
Outputs:
  data/sft/lf_{snapshot}.parquet                (LLaMA-Factory ingest)
  data/sft/lf_{snapshot}_dataset_info.json      (snippet for LLaMA-Factory)
  data/sft/lf_{snapshot}_system_prompt.txt      (the shared system prompt)

Usage
-----
    python3 scripts/convert_to_llamafactory.py --snapshot pilot400_v1
    python3 scripts/convert_to_llamafactory.py --snapshot phase_c_v1
    python3 scripts/convert_to_llamafactory.py --snapshot pilot400_v1 --keep-system-in-rows
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_SFT_DIR = os.path.join(REPO_ROOT, "data/sft")

# Mapping from our schema_v1_1 ChatML roles to ShareGPT-style role tags.
# These are the conventional ShareGPT names that every recent LLaMA-Factory
# release recognizes via its default `tags` block.
ROLE_MAP = {
    "system": "system",
    "user": "human",
    "assistant": "gpt",
    "tool": "observation",
}


def convert_messages_to_sharegpt(messages: list[dict], strip_system: bool) -> tuple[list[dict], str | None]:
    """Convert a list of {role, content} messages to a list of {from, value}.

    Returns (sharegpt_messages, system_prompt_or_none).
    If strip_system=True, the leading system message is removed and returned
    separately so it can be stored in a shared `system` column.
    """
    out: list[dict] = []
    extracted_system: str | None = None
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            if strip_system:
                if extracted_system is None:
                    extracted_system = content
                continue
        sg_role = ROLE_MAP.get(role)
        if sg_role is None:
            raise ValueError(f"unknown role {role!r}")
        out.append({"from": sg_role, "value": content})
    return out, extracted_system


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, help="snapshot id, e.g. pilot400_v1")
    parser.add_argument("--sft-dir", default=DEFAULT_SFT_DIR)
    parser.add_argument(
        "--keep-system-in-rows",
        action="store_true",
        help="keep the system prompt as a `system` row in conversations (default: extract to a shared column)",
    )
    parser.add_argument(
        "--dataset-name",
        default=None,
        help="LLaMA-Factory dataset name in dataset_info.json (default: multiagent_router_{snapshot})",
    )
    args = parser.parse_args()

    import pandas as pd  # noqa: WPS433

    src = os.path.join(args.sft_dir, f"train_{args.snapshot}.parquet")
    if not os.path.exists(src):
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 2

    df = pd.read_parquet(src)
    print(f"loaded {len(df)} rows from {src}")

    # Verify all systems are identical (when stripping)
    if not args.keep_system_in_rows:
        sys_hashes = set()
        for msgs in df["messages"]:
            if msgs[0]["role"] == "system":
                sys_hashes.add(hashlib.sha256(msgs[0]["content"].encode()).hexdigest())
        if len(sys_hashes) > 1:
            print(
                f"WARNING: {len(sys_hashes)} distinct system prompts; "
                "cannot extract to a shared column. Re-run with --keep-system-in-rows.",
                file=sys.stderr,
            )
            return 3

    # Convert each row
    new_rows = []
    shared_system: str | None = None
    for _, row in df.iterrows():
        sg, sys_p = convert_messages_to_sharegpt(
            list(row["messages"]),
            strip_system=not args.keep_system_in_rows,
        )
        if shared_system is None and sys_p is not None:
            shared_system = sys_p
        out_row = {
            "conversations": sg,
            "id": row["id"],
            "source": row["source"],
            "domain": row["domain"],
            "behavior": row["behavior"],
            "repair_type": row["repair_type"],
            "behavior_match": bool(row["behavior_match"]),
            "n_plan_rounds": int(row["n_plan_rounds"]),
            "n_routes": int(row["n_routes"]),
            "n_subtasks": int(row["n_subtasks"]),
            "models_used": list(row["models_used"]),
            "skills_used": list(row["skills_used"]),
            "gold": row["gold"],
            "teacher": row["teacher"],
            "n_attempts": int(row["n_attempts"]),
        }
        if not args.keep_system_in_rows and shared_system is not None:
            # LLaMA-Factory uses the `system` column when present
            out_row["system"] = shared_system
        new_rows.append(out_row)

    out_df = pd.DataFrame(new_rows)
    out_parquet = os.path.join(args.sft_dir, f"lf_{args.snapshot}.parquet")
    out_df.to_parquet(out_parquet, index=False)
    print(f"wrote {out_parquet}  ({len(out_df)} rows, {os.path.getsize(out_parquet)/1024:.0f} KB)")

    # Save the shared system prompt as a separate text file for inspection / inference time
    if shared_system is not None:
        sys_path = os.path.join(args.sft_dir, f"lf_{args.snapshot}_system_prompt.txt")
        with open(sys_path, "w") as f:
            f.write(shared_system)
        print(f"wrote {sys_path}  ({len(shared_system)} chars)")

    # Build dataset_info.json snippet
    dataset_name = args.dataset_name or f"multiagent_router_{args.snapshot}"
    rel_parquet = os.path.relpath(out_parquet, REPO_ROOT)
    dataset_info_snippet = {
        dataset_name: {
            "file_name": rel_parquet,
            "formatting": "sharegpt",
            "columns": {
                "messages": "conversations",
                **({"system": "system"} if not args.keep_system_in_rows else {}),
            },
            "tags": {
                "role_tag": "from",
                "content_tag": "value",
                "user_tag": "human",
                "assistant_tag": "gpt",
                "system_tag": "system",
                "observation_tag": "observation",
            },
        }
    }
    info_path = os.path.join(args.sft_dir, f"lf_{args.snapshot}_dataset_info.json")
    with open(info_path, "w") as f:
        json.dump(dataset_info_snippet, f, indent=2, ensure_ascii=False)
    print(f"wrote {info_path}")

    # Sanity report
    print()
    print("=" * 78)
    print(f"snapshot:        {args.snapshot}")
    print(f"dataset name:    {dataset_name}")
    print(f"rows:            {len(out_df)}")
    print(f"system prompt:   {'shared column' if not args.keep_system_in_rows else 'inlined per row'}")
    if shared_system is not None:
        print(f"system length:   {len(shared_system)} chars")
    print(f"role mapping:    {ROLE_MAP}")
    print()
    print("Sample row 0 conversations preview:")
    sample_conv = new_rows[0]["conversations"]
    for i, m in enumerate(sample_conv):
        preview = m["value"][:80].replace("\n", " ")
        print(f"  [{i}] from={m['from']:<12} value={preview!r}{'…' if len(m['value']) > 80 else ''}")
    print()
    print("LLaMA-Factory dataset_info.json snippet:")
    print(json.dumps(dataset_info_snippet, indent=2, ensure_ascii=False))
    print()
    print("To use:")
    print(f"  1. cp {info_path} ~/LlamaFactory/data/dataset_info.json  (or merge into existing)")
    print(f"  2. cp {out_parquet} ~/LlamaFactory/data/")
    print(f"  3. In LLaMA-Factory, select dataset name: {dataset_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
