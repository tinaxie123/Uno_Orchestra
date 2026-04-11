"""
Extract (question, gold) pairs from SFT data to create RL prompt pool.

Usage:
    python scripts/rl/prepare_prompt_pool.py \
        --input /path/to/train_final.parquet \
        --system-prompt /path/to/system_prompt.txt \
        --output-train /path/to/rl_train.parquet \
        --output-val /path/to/rl_val.parquet \
        --val-ratio 0.05
"""

import argparse
import json
import re

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


FINAL_RE = re.compile(r"<final_answer>(.*?)</final_answer>", re.DOTALL)


def extract_question_and_gold(messages):
    """Extract question from user turn and gold from the trajectory."""
    if hasattr(messages, "tolist"):
        messages = messages.tolist()

    question = None
    gold = None

    for m in messages:
        if m["role"] == "user":
            # Extract the question part (before behavioral hints)
            content = m["content"]
            # Try to extract just the question
            if "Question:" in content:
                q = content.split("Question:")[-1]
                # Remove behavioral hints
                for marker in ["\n\nCorrect answer", "\n\nBEHAVIORAL HINT", "\n\nOutput the trajectory"]:
                    if marker in q:
                        q = q[:q.index(marker)]
                question = q.strip()
            else:
                question = content.strip()

        if m["role"] == "assistant":
            match = FINAL_RE.search(m["content"])
            if match:
                gold = match.group(1).strip()

    return question, gold


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Path to train_final.parquet")
    parser.add_argument("--system-prompt", required=True, help="Path to system_prompt.txt")
    parser.add_argument("--output-train", required=True)
    parser.add_argument("--output-val", required=True)
    parser.add_argument("--val-ratio", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    df = pd.read_parquet(args.input)
    print(f"Loaded {len(df)} samples")

    with open(args.system_prompt) as f:
        system_prompt = f.read()

    rows = []
    skipped = 0
    for idx in range(len(df)):
        messages = df["messages"].iloc[idx]
        question, gold = extract_question_and_gold(messages)

        if not question or not gold:
            skipped += 1
            continue

        # Get domain info
        domain = df["domain"].iloc[idx] if "domain" in df.columns else "unknown"
        source = df["source"].iloc[idx] if "source" in df.columns else "unknown"

        row = {
            "prompt": json.dumps([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Question: {question}\n\nOutput the trajectory now."},
            ]),
            "data_source": str(domain),
            "ability": "routing",
            "reward_model": json.dumps({"ground_truth": gold}),
            "extra_info": json.dumps({
                "question": question,
                "gold": gold,
                "source": str(source),
            }),
            "env_kwargs": json.dumps({
                "question": question,
                "ground_truth": gold,
                "data_source": str(domain),
            }),
        }
        rows.append(row)

    print(f"Extracted {len(rows)} (question, gold) pairs, skipped {skipped}")

    # Split train/val
    import random
    random.seed(args.seed)
    random.shuffle(rows)
    val_size = int(len(rows) * args.val_ratio)
    val_rows = rows[:val_size]
    train_rows = rows[val_size:]

    # Save as parquet
    for path, data, name in [
        (args.output_train, train_rows, "train"),
        (args.output_val, val_rows, "val"),
    ]:
        table = pa.table({k: [r[k] for r in data] for k in data[0].keys()})
        pq.write_table(table, path)
        print(f"Saved {len(data)} {name} samples to {path}")


if __name__ == "__main__":
    main()
