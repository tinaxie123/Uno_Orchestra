# Uno-Orchestra Evaluation Framework

This repository provides a reproducible evaluation and routing framework for
Uno-Orchestra-style selective delegation. A single policy model decides whether
to answer directly or decompose a task, then routes each subtask to an
admissible `(worker model, primitive)` pair. Worker calls are executed through a
shared harness, and evaluation reports both task quality and inference cost.

The implementation follows the evaluation design of
[Uno-Orchestra: Parsimonious Agent Routing via Selective Delegation](https://arxiv.org/html/2605.05007v1).

## What This Repo Contains

- A unified Uno router interface using the `<plan>`, `<route>`, `<obs>`,
  `<verify>`, and `<final_answer>` schema.
- A closed worker-model and primitive pool in [configs/pools.yaml](configs/pools.yaml).
- A 13-benchmark evaluation suite with `pass@1`, `pass@2`, domain macro, and
  13-benchmark macro reporting.
- Two complete scoring modes:
  `official_compatible` and `uno_harness`.
- OpenAI-compatible endpoint integration for local policy checkpoints and
  remote/local worker gateways.
- Docker harness support for SWE-bench and Terminal-Bench.
- Preflight checks, smoke evaluation, LiteLLM gateway template, and result
  collection scripts.

## Architecture

```text
User task
  |
  v
Uno policy / router checkpoint  (LOCAL_BASE)
  |
  | emits either:
  |   <final_answer>...</final_answer>
  | or:
  |   <plan round="...">...</plan>
  |   <route round="..." subtask="..." model="..." skill="...">...</route>
  v
Route harness
  |
  +-- validates model/primitive pair against configs/pools.yaml
  +-- dispatches local primitives: execute_python, execute_shell, symbolic_math, ...
  +-- dispatches worker LLM calls through API_BASE
  |
  v
<obs> worker observations
  |
  v
Uno policy repairs, routes again, or emits <final_answer>
  |
  v
Benchmark verifier and summary.json
```

The old CLI name `--router planner` is kept for compatibility. In this
repository it points to the unified Uno policy router.

## Evaluation Suite

The full suite contains 13 benchmarks across five domains.

| Domain | Benchmarks |
| --- | --- |
| Math | MATH-500, AIME |
| Code / software engineering | HumanEval, MBPP, LiveCodeBench, SWE-bench |
| Knowledge / scientific reasoning | MMLU, GPQA |
| Reading / long context | DROP, MRCR |
| Agentic / tool use | GAIA, Terminal-Bench, ToolBench |

`scripts/collect_results.py` reports:

- per-benchmark `pass@1` and `pass@2`
- 13-benchmark macro `pass@1/pass@2`
- 5-domain macro `pass@1/pass@2`
- average context tokens
- average output tokens
- average USD/query
- scoring-mode mix per model

## Scoring Modes

Every `summary.json` includes `scoring_mode` and `score_name`.

| Mode | Score name | Benchmarks |
| --- | --- | --- |
| `official_compatible` | Official-compatible score | SWE-bench one-shot, HumanEval, MBPP, LiveCodeBench, MMLU, GPQA, MATH-500, AIME, DROP |
| `uno_harness` | Uno harness score | SWE-bench interactive, Terminal-Bench, GAIA, ToolBench, MRCR |

The default full-suite script runs SWE-bench and Terminal-Bench with the
interactive Uno harness path.

## Repository Layout

```text
uno_orchestor/
  routing/uno/                 Uno primitives, route harness, worker backends
  agents/                      Sub-agent execution helpers

env/
  env_package/uno/             RL/eval environment and verifiers

configs/
  pools.yaml                   Worker models, primitive vocabulary, prices
  uno/system_prompt.txt        Default Uno schema prompt
  litellm.example.yaml         Example worker gateway config

eval_pipeline/
  benchmarks/                  13 benchmark adapters
  routers/                     Direct, random, oracle, Uno policy routers
  executors/                   Docker, SWE-bench, Terminal-Bench executors
  run.py                       Main evaluation runner

scripts/
  run_full_eval.sh             13-benchmark evaluation matrix
  run_smoke_eval.sh            One-task smoke evaluation
  check_eval_env.py            Environment preflight checks
  collect_results.py           Paper-style result aggregation

examples/
  run_gpqa_smoke.sh            Minimal copyable evaluation example
```

## Installation

Use Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[eval]'
```

For Docker benchmarks:

```bash
pip install -e '.[eval,docker]'
```

For serving a local policy checkpoint with vLLM:

```bash
pip install -e '.[serve]'
```

For a LiteLLM worker gateway:

```bash
pip install -e '.[gateway]'
```

More environment details are in
[docs/evaluation_environment.md](docs/evaluation_environment.md).

## Configure Endpoints

Copy the environment template:

```bash
cp .env.example .env
```

Set these core variables:

```bash
LOCAL_BASE=http://localhost:8000/v1
LOCAL_MODEL=Qwen/Qwen2.5-7B-Instruct
API_BASE=http://localhost:9000/v1
API_KEY=EMPTY
PASS_K=2
```

`LOCAL_BASE` serves the Uno policy/router checkpoint. `API_BASE` serves worker
models from [configs/pools.yaml](configs/pools.yaml). Both endpoints must be
OpenAI-compatible.

### Local Policy Server

Example with vLLM:

```bash
python -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 \
  --port 8000 \
  --model Qwen/Qwen2.5-7B-Instruct \
  --served-model-name Qwen/Qwen2.5-7B-Instruct
```

For your SFT/RL checkpoint, replace `--model` and `LOCAL_MODEL` with the
checkpoint path or served name.

### Worker Gateway

The worker gateway can be LiteLLM, vLLM, or any OpenAI-compatible gateway.
A LiteLLM template is provided:

```bash
litellm --config configs/litellm.example.yaml --host 0.0.0.0 --port 9000
```

Make sure the gateway accepts the model ids listed in `configs/pools.yaml`.

## Preflight

Check Python packages and endpoint availability:

```bash
python scripts/check_eval_env.py
```

Check Docker, SWE-bench, and Terminal-Bench readiness:

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

This runs one GPQA sample through the Uno router and prints a collector table.

## Full Evaluation

Run the full 13-benchmark matrix:

```bash
PASS_K=2 bash scripts/run_full_eval.sh
```

Run a subset:

```bash
bash scripts/run_full_eval.sh --bench gpqa,mmlu,math500
```

Collect model-level metrics:

```bash
python scripts/collect_results.py --root data/eval --format md
```

JSON and CSV are also supported:

```bash
python scripts/collect_results.py --root data/eval --format json
python scripts/collect_results.py --root data/eval --format csv
```

## Outputs

Each run writes:

```text
data/eval/<model_name>/<benchmark>/
  predictions.jsonl
  verification.jsonl
  summary.json
  logs/
```

`summary.json` contains:

- router and benchmark names
- `scoring_mode` and `score_name`
- `pass_at_1` and `pass_at_2`
- total and average USD cost
- total and average tokens
- routed model / skill / backend usage
- passed task ids

## Training Components

The repository also contains the Uno RL environment and reward integration used
for SFT/RL workflows:

- `env/env_package/uno/`
- `uno_orchestor/routing/uno/`
- `scripts/rl/`
- `configs/rl/`
- vendored `verl/`

The evaluation framework can be used independently as long as a policy
checkpoint is served through `LOCAL_BASE`.

## Citation

This repository is organized around the framework and evaluation protocol from:

```bibtex
@article{cui2026unoorchestra,
  title = {Uno-Orchestra: Parsimonious Agent Routing via Selective Delegation},
  author = {Cui, Zhiqing and Xie, Haotong and Yuan, Jiahao and Yang, Cheng and Wang, Hanqing and Wu, Yuxin and Wu, Yifan and Zhong, Siru and Yu, Tao and Guo, Yifu and Zhang, Siyu and Yu, Xinlei and Ren, Qibing and Naseem, Usman},
  journal = {arXiv preprint arXiv:2605.05007},
  year = {2026},
  url = {https://arxiv.org/html/2605.05007v1}
}
```

Paper: <https://arxiv.org/html/2605.05007v1>
