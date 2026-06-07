# Evaluation Environment

This page describes the runtime pieces needed to reproduce the paper-style
evaluation suite.

## Install

Create a Python 3.10+ environment and install the base evaluator:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[eval]'
```

For SWE-bench / Terminal-Bench Docker evaluations:

```bash
pip install -e '.[eval,docker]'
```

For serving a local checkpoint with vLLM:

```bash
pip install -e '.[serve]'
```

## Environment File

Copy the template and edit it:

```bash
cp .env.example .env
```

Important variables:

- `LOCAL_BASE`: OpenAI-compatible endpoint for the local Uno policy/router.
- `LOCAL_MODEL`: model id served at `LOCAL_BASE`.
- `API_BASE`: OpenAI-compatible endpoint for worker models in `configs/pools.yaml`.
- `API_KEY`: worker gateway API key, or `EMPTY` for local gateways.
- `TERMINAL_BENCH_TASKS_DIR`: local Terminal-Bench task package root.
- `UNO_SYSTEM_PROMPT`: optional override for `configs/uno/system_prompt.txt`.

## Local Policy Endpoint

The local endpoint serves the policy checkpoint that emits Uno route schema.
For a vLLM-compatible Hugging Face checkpoint:

```bash
python -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 \
  --port 8000 \
  --model Qwen/Qwen2.5-7B-Instruct \
  --served-model-name Qwen/Qwen2.5-7B-Instruct
```

Then set:

```bash
LOCAL_BASE=http://localhost:8000/v1
LOCAL_MODEL=Qwen/Qwen2.5-7B-Instruct
```

For your SFT/RL checkpoint, replace `--model` and `LOCAL_MODEL` with the
checkpoint path or served model name.

## Worker Gateway

`API_BASE` must expose an OpenAI-compatible `/v1/chat/completions` API for the
worker ids in `configs/pools.yaml`, for example:

- a LiteLLM proxy
- an internal OpenAI-compatible model gateway
- a local vLLM server if you reduce `configs/pools.yaml` to local models

Minimal local single-worker setup:

1. Edit `configs/pools.yaml` so `models` contains the served worker model id.
2. Serve that model with vLLM on port 9000.
3. Set `API_BASE=http://localhost:9000/v1`.

For multi-provider gateways, ensure `/v1/models` lists or accepts these ids:

```bash
python - <<'PY'
from configs import load_pools
print("\n".join(load_pools()["models"]))
PY
```

A LiteLLM proxy template is provided at `configs/litellm.example.yaml`:

```bash
pip install -e '.[gateway]'
litellm --config configs/litellm.example.yaml --host 0.0.0.0 --port 9000
```

If you enable `general_settings.master_key` in that file, set `API_KEY` to the
same value in `.env`.

## Docker

Install Docker with Compose v2:

```bash
docker version
docker compose version
```

Terminal-Bench tasks are built or pulled through
`eval_pipeline/executors/docker-compose-build.yaml`. If containers need outbound
network through a proxy, set `TBENCH_HTTP_PROXY`, `TBENCH_HTTPS_PROXY`, and
`TBENCH_ALL_PROXY` in `.env`. No proxy is injected by default.

## SWE-bench

SWE-bench Verified uses:

- Hugging Face dataset `princeton-nlp/SWE-bench_Verified`
- official `swebench/sweb.eval.*` Docker images
- optional official `swebench` Python package for one-shot harness mode

Before large runs, make sure Docker can pull the official images. Interactive
SWE-bench runs use `SWEBenchExecutor`; one-shot patch verification uses the
official harness if available.

## Terminal-Bench

The evaluator expects a local task package directory containing per-task
subdirectories with:

- `task.toml`
- `instruction.md`
- `tests/test.sh`
- `environment/Dockerfile`, unless `task.toml` points at a prebuilt image

Set:

```bash
TERMINAL_BENCH_TASKS_DIR=/path/to/terminal-bench/tasks
```

## Preflight

Check the base environment:

```bash
python scripts/check_eval_env.py
```

Check Docker and Terminal-Bench paths:

```bash
python scripts/check_eval_env.py --docker
```

Check representative Hugging Face datasets:

```bash
python scripts/check_eval_env.py --datasets --skip-endpoints
```

## Smoke Evaluation

After `LOCAL_BASE` and `API_BASE` are running:

```bash
bash scripts/run_smoke_eval.sh
```

This runs one GPQA sample through the Uno router and prints the collector table.

## Full Suite

```bash
PASS_K=2 bash scripts/run_full_eval.sh
python scripts/collect_results.py --root data/eval --format md
```

## Scoring Modes

The evaluation suite is complete in two scoring modes:

- `official_compatible`: benchmark-standard answer extraction and verifier
  flow for SWE-bench one-shot, HumanEval, MBPP, LiveCodeBench, MMLU, GPQA,
  MATH-500, AIME, and DROP.
- `uno_harness`: multi-turn Uno harness flow for SWE-bench interactive,
  Terminal-Bench, GAIA, ToolBench, and MRCR.

The runner writes `scoring_mode` and `score_name` to every `summary.json`.
The collector includes the scoring-mode mix in the model-level table.
