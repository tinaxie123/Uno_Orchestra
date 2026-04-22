"""
Convert merged SFT parquet to LlamaFactory sharegpt JSON.

The parquet exposes a ``conversations_raw`` column (a JSON-encoded list of
``{"from", "value"}`` entries in ShareGPT format). LlamaFactory's
``router_sft_v2`` dataset entry expects rows with key ``conversations``.

Usage:
    python scripts/sft/prepare_data.py \
        --input data/sft/merged/sft.parquet \
        --output /home/xieht/data/LlamaFactory/data/router_sft_sharegpt.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

VALID_ROLES = {"system", "human", "gpt", "observation", "function_call"}


def parse_conversation(raw) -> list[dict] | None:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if hasattr(raw, "tolist"):
        return raw.tolist()
    if isinstance(raw, list):
        return raw
    return None


def convert(input_path: Path, output_path: Path) -> None:
    df = pd.read_parquet(input_path)
    print(f"loaded {len(df)} rows from {input_path}")
    if "conversations_raw" not in df.columns:
        sys.exit(f"expected column 'conversations_raw' not found: {list(df.columns)}")

    output: list[dict] = []
    dropped_parse = 0
    dropped_role = 0
    dropped_empty = 0
    for idx, raw in enumerate(df["conversations_raw"]):
        conv = parse_conversation(raw)
        if conv is None:
            dropped_parse += 1
            continue
        if not conv:
            dropped_empty += 1
            continue
        bad = False
        for msg in conv:
            if msg.get("from") not in VALID_ROLES:
                bad = True
                break
        if bad:
            dropped_role += 1
            continue
        output.append({"conversations": conv})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(
        f"wrote {len(output)} rows to {output_path} "
        f"(dropped_parse={dropped_parse}, dropped_role={dropped_role}, dropped_empty={dropped_empty})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    convert(Path(args.input), Path(args.output))
