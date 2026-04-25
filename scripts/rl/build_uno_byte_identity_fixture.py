#!/usr/bin/env python3
"""Build UNO byte-identity fixture from a trajectory JSON.

Input schema:
{
  "raw_prompt": [{"role": "...", "content": "..."}, ...],
  "assistant_turn_texts": ["...", "..."],
  "obs_turn_texts": ["...", ...]
}

Output extends the input with:
  - expected_prompt_ids
  - expected_response_ids
  - expected_response_mask
  - expected_full_ids
  - expected_canonical_full_ids
  - tokenizer_path
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from transformers import AutoTokenizer


def _initialize_system_prompt(tokenizer) -> list[int]:
    token1 = tokenizer.apply_chat_template(
        [{"role": "user", "content": ""}], add_generation_prompt=False, tokenize=True
    )
    token2 = tokenizer.apply_chat_template(
        [{"role": "user", "content": ""}] * 2, add_generation_prompt=False, tokenize=True
    )
    return token1[: -(len(token2) - len(token1))]


def _canonical_tokenize(tokenizer, raw_prompt, assistant_turn_texts, obs_turn_texts):
    messages = list(raw_prompt)
    for idx, assistant_text in enumerate(assistant_turn_texts):
        messages.append({"role": "assistant", "content": assistant_text})
        if idx < len(obs_turn_texts):
            messages.append({"role": "user", "content": obs_turn_texts[idx]})
    return tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
    )


def _build_expected(
    tokenizer,
    raw_prompt,
    assistant_turn_texts,
    obs_turn_texts,
    policy_turn_token_ids=None,
):
    system_prompt_ids = _initialize_system_prompt(tokenizer)
    prompt_ids = tokenizer.apply_chat_template(
        raw_prompt,
        add_generation_prompt=True,
        tokenize=True,
    )
    full_ids = list(prompt_ids)
    response_ids = []
    response_mask = []

    for i, assistant_text in enumerate(assistant_turn_texts):
        if policy_turn_token_ids is not None:
            turn_ids = list(policy_turn_token_ids[i])
        else:
            turn_ids = tokenizer.encode(assistant_text, add_special_tokens=False)
        full_ids.extend(turn_ids)
        response_ids.extend(turn_ids)
        response_mask.extend([1] * len(turn_ids))

        if i < len(obs_turn_texts):
            obs_ids = tokenizer.apply_chat_template(
                [{"role": "user", "content": obs_turn_texts[i]}],
                add_generation_prompt=True,
                tokenize=True,
            )
            obs_ids = obs_ids[len(system_prompt_ids) :]
            full_ids.extend(obs_ids)
            response_ids.extend(obs_ids)
            response_mask.extend([0] * len(obs_ids))

    return prompt_ids, response_ids, response_mask, full_ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Input trajectory JSON path")
    parser.add_argument("--output", required=True, help="Output fixture JSON path")
    parser.add_argument("--tokenizer-path", required=True, help="HF/local tokenizer path")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    data = json.loads(in_path.read_text(encoding="utf-8"))

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path, trust_remote_code=True)
    policy_turn_token_ids = data.get("policy_turn_token_ids")
    prompt_ids, response_ids, response_mask, full_ids = _build_expected(
        tokenizer,
        data["raw_prompt"],
        data["assistant_turn_texts"],
        data.get("obs_turn_texts", []),
        policy_turn_token_ids=policy_turn_token_ids,
    )
    canonical_full_ids = _canonical_tokenize(
        tokenizer,
        data["raw_prompt"],
        data["assistant_turn_texts"],
        data.get("obs_turn_texts", []),
    )

    data["tokenizer_path"] = args.tokenizer_path
    data["expected_prompt_ids"] = prompt_ids
    data["expected_response_ids"] = response_ids
    data["expected_response_mask"] = response_mask
    data["expected_full_ids"] = full_ids
    data["expected_canonical_full_ids"] = canonical_full_ids

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote fixture: {out_path}")


if __name__ == "__main__":
    main()
