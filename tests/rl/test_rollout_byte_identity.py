import json
import os
from pathlib import Path

import pytest


FIXTURE_PATH = Path("tests/rl/fixtures/sft_golden_trajectory.json")


def _resolve_tokenizer_path() -> Path | None:
    candidates = [
        os.environ.get("UNO_TOKENIZER_PATH"),
        "/data/xieht/checkpoints/Uno-Orchestra-7B-SFT",
        "/data/xieht/LlamaFactory/outputs/router_qwen25_7b_sft",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return None


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


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


def _incremental_rollout_rebuild(
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

    for idx, assistant_text in enumerate(assistant_turn_texts):
        if policy_turn_token_ids is not None:
            turn_ids = list(policy_turn_token_ids[idx])
        else:
            turn_ids = tokenizer.encode(assistant_text, add_special_tokens=False)
        full_ids.extend(turn_ids)
        response_ids.extend(turn_ids)
        response_mask.extend([1] * len(turn_ids))

        if idx < len(obs_turn_texts):
            obs_ids = tokenizer.apply_chat_template(
                [{"role": "user", "content": obs_turn_texts[idx]}],
                add_generation_prompt=True,
                tokenize=True,
            )
            # Match UnoAgentLoop(remove_system_prompt=True).
            obs_ids = obs_ids[len(system_prompt_ids) :]
            full_ids.extend(obs_ids)
            response_ids.extend(obs_ids)
            response_mask.extend([0] * len(obs_ids))

    return prompt_ids, response_ids, response_mask, full_ids


def test_rollout_byte_identity():
    transformers = pytest.importorskip("transformers")
    AutoTokenizer = transformers.AutoTokenizer

    tokenizer_path = _resolve_tokenizer_path()
    if tokenizer_path is None:
        pytest.skip("Tokenizer path not found; set UNO_TOKENIZER_PATH to run byte-identity test.")

    fx = _load_fixture()
    raw_prompt = fx["raw_prompt"]
    assistant_turn_texts = fx["assistant_turn_texts"]
    obs_turn_texts = fx["obs_turn_texts"]
    policy_turn_token_ids = fx.get("policy_turn_token_ids")

    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path), trust_remote_code=True)

    canonical_ids = _canonical_tokenize(
        tokenizer=tokenizer,
        raw_prompt=raw_prompt,
        assistant_turn_texts=assistant_turn_texts,
        obs_turn_texts=obs_turn_texts,
    )
    prompt_ids, response_ids, response_mask, rebuilt_full_ids = _incremental_rollout_rebuild(
        tokenizer=tokenizer,
        raw_prompt=raw_prompt,
        assistant_turn_texts=assistant_turn_texts,
        obs_turn_texts=obs_turn_texts,
        policy_turn_token_ids=policy_turn_token_ids,
    )

    expected_prompt_ids = fx.get("expected_prompt_ids")
    expected_response_ids = fx.get("expected_response_ids")
    expected_response_mask = fx.get("expected_response_mask")
    expected_full_ids = fx.get("expected_full_ids")
    if not all(v is not None for v in (expected_prompt_ids, expected_response_ids, expected_response_mask, expected_full_ids)):
        pytest.skip("Fixture missing expected_* fields; regenerate with build_uno_byte_identity_fixture.py.")

    assert prompt_ids == expected_prompt_ids
    assert response_ids == expected_response_ids
    assert response_mask == expected_response_mask
    assert rebuilt_full_ids == expected_full_ids

    expected_canonical_full_ids = fx.get("expected_canonical_full_ids")
    if expected_canonical_full_ids is not None:
        assert canonical_ids == expected_canonical_full_ids

    if fx.get("enforce_canonical_incremental_equal", False):
        assert canonical_ids == rebuilt_full_ids

    assert len(response_ids) == len(response_mask)
    assert 1 in response_mask
    assert max(i for i, v in enumerate(response_mask) if v == 1) >= 0
