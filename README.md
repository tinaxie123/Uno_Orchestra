# SkillRouter: Learned Selective Delegation for Multi-Agent Systems

## Overview

Large language models are increasingly deployed as agents that decompose complex tasks into subtasks and delegate them to specialized workers. However, current multi-agent frameworks apply fixed decomposition strategies -- either always decompose (incurring unnecessary cost) or never decompose (losing the benefit of specialization).

We propose **SkillRouter**, a lightweight trainable router (7B parameters) that learns **selective delegation** -- deciding per-query whether to solve directly or decompose into a DAG of (model, skill) pairs. The router is trained via SFT on distilled multi-agent trajectories, followed by RL (GiGPO) with a cost-aware reward signal.

**Key contributions**:
1. **Structured trajectory schema** (v1.1): `<plan>` -> `<route>` -> `<obs>` -> `<verify>` -> `<final_answer>`, supporting lazy (direct answer), one-shot decomposition, multi-round continuation, and decomposition repair.
2. **Evidence-grounded distillation**: 58,457 training trajectories across 9 domains (31 datasets), with real evidence injected from dataset context fields.
3. **Cost-aware RL**: GiGPO with two-level advantage decomposition (episode + step), reward = correctness - lambda x cost. The router learns to avoid unnecessary delegation on easy tasks while decomposing hard ones.
4. **Unified evaluation pipeline**: Modular framework supporting multiple routers and benchmarks with official verification (swebench harness, Harbor Docker).

## Method

```
Query -> SkillRouter (7B)
           |
           |--> Lazy mode: <final_answer> directly (no delegation)
           |
           |--> Plan mode:
                  <plan round="1">
                    <subtask id="1" depends_on="">...</subtask>
                    <subtask id="2" depends_on="1">...</subtask>
                  </plan>
                  <route model="gpt-5.3-codex" skill="execute_python">...</route>
                  <route model="claude-haiku" skill="web_search">...</route>
                       |
                       v
                  Real API calls to 9 worker models x 13 skills
                       |
                       v
                  <obs subtask="1">result</obs>
                  <verify round="1" status="pass">...</verify>
                  <final_answer>...</final_answer>
```

**Worker pool**: 9 models across 5 families (Anthropic, OpenAI, Google, Moonshot, Alibaba), 13 skills (direct_answer, reason, web_search, execute_python, execute_shell, etc.). See `configs/pools.yaml`.

**Training pipeline**: Teacher distillation (Phase C) -> SFT warm-start (Phase D) -> GiGPO RL (Phase E) -> Evaluation (Phase F) -> Ablations (Phase G).

## Repository Structure

```
multiagentRL/
  README.md                    # This file
  configs/
    pools.yaml                 # 9 models x 13 skills (worker pool definition)
    sft/                       # SFT training configs (LlamaFactory + DeepSpeed)
  docs/
    experiment_plan.md         # Full experiment plan (data, training, eval, ablations)
    schema.md                  # Trajectory schema v1.1 specification
    pipeline.md                # Data distillation and training pipeline
    eval_status.md             # Live evaluation progress report
  scripts/
    data/                      # Distillation: generate_trajectories, validate, build_dataset
    sft/                       # SFT training: prepare_data, run_sft
    rl/                        # RL training: prepare_prompt_pool, run_gigpo
  eval_pipeline/               # Unified evaluation framework
    config.py                  # Single source: model pool, costs, skills
    run.py                     # Entry: python -m eval_pipeline.run --router X --bench Y
    routers/                   # Router adapters (router-r1, skillrouter-sft, direct, random, oracle)
    benchmarks/                # Benchmark adapters (swebench, terminalbench)
  agent_system/
    environments/env_package/skillrouter/
      envs.py                  # RL environment with real API sub-agent calls
      env_manager.py           # verl-agent interface
      projection.py            # Output format validation
  data/
    sft_recipe.yaml            # 31 datasets, 10 domains, sample counts
```

## Quick Start

### Evaluation (any router x any benchmark)

```bash
cd multiagentRL

# Router-R1 baseline on SWE-bench
python -m eval_pipeline.run --router router-r1 --bench swebench --api_key KEY

# SkillRouter SFT on Terminal-Bench
python -m eval_pipeline.run --router skillrouter-sft --bench terminalbench \
    --local_base http://localhost:8000/v1 --local_model SkillRouter-SFT --api_key KEY

# Direct(GPT-5.4) upper bound
python -m eval_pipeline.run --router direct --bench swebench --direct_model gpt-5.4 --api_key KEY

# All available routers:
#   router-r1, skillrouter-sft, direct, random,
#   oracle-cheapest, oracle-strongest, oracle-codex
```

### Training

```bash
# Phase C: Distillation
python scripts/data/generate_trajectories.py --full --concurrency 200

# Phase D: SFT
bash scripts/sft/run_sft.sh

# Phase E: RL (GiGPO)
bash scripts/rl/run_gigpo_skillrouter.sh
```

## Current Status

| Phase | Task | Status |
|-------|------|--------|
| C | Distillation (58,457 trajectories) | Done |
| D | SFT warm-start (Qwen2.5-7B) | Done |
| E | RL fine-tuning (GiGPO, 100 steps) | In progress |
| F | Baseline evaluation (8 baselines x 2 benchmarks) | In progress |
| G | Ablation studies | Planned |

See `docs/experiment_plan.md` for full details and `docs/eval_status.md` for live eval progress.
