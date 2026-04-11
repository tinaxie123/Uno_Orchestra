"""
Convert SFT parquet data to LlamaFactory sharegpt format.

Input:  data/sft/train_final.parquet (schema v1.1 validated)
Output: LlamaFactory/data/router_sft_sharegpt.json

Usage:
    python scripts/sft/prepare_data.py \
        --input /path/to/train_final.parquet \
        --output /path/to/LlamaFactory/data/router_sft_sharegpt.json
"""

import argparse
import json
import pandas as pd


ROLE_MAP = {
    "system": "system",
    "user": "human",
    "assistant": "gpt",
    "tool": "observation",
}


def convert_parquet_to_sharegpt(input_path: str, output_path: str):
    df = pd.read_parquet(input_path)
    print(f"Loaded {len(df)} samples from {input_path}")

    output = []
    for idx in range(len(df)):
        msgs = df["messages"].iloc[idx]
        if hasattr(msgs, "tolist"):
            msgs = msgs.tolist()

        converted = []
        for m in msgs:
            role = ROLE_MAP.get(m["role"])
            if role is None:
                print(f"WARNING: unknown role '{m['role']}' in sample {idx}, skipping")
                break
            converted.append({"from": role, "value": m["content"]})
        else:
            output.append({"conversations": converted})

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    print(f"Saved {len(output)} samples to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to train_final.parquet")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()
    convert_parquet_to_sharegpt(args.input, args.output)
