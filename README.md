# SkillRouter: Learned Selective Delegation for Multi-Agent Systems

## Overview

Large language models are increasingly deployed as agents that decompose complex tasks and delegate them to specialized workers. However, current multi-agent frameworks apply fixed decomposition strategies -- either always decompose (wasting compute on simple tasks) or never decompose (losing the benefit of specialization on hard tasks).

We propose **SkillRouter**, a compact trainable router (7B) that learns **selective delegation** -- deciding per-query whether to solve directly or decompose into a DAG of subtasks, each routed to a specific (model, skill) pair from a heterogeneous worker pool. Training proceeds in two stages:

1. **SFT warm-start** on 58K distilled multi-agent trajectories across 9 domains
2. **GiGPO RL** with cost-aware reward: `R = (1-a) * correctness + a * cost_efficiency`

The router learns four behaviors: lazy (direct answer), one-shot decomposition, observation-driven continuation, and decomposition repair -- all within a single 7B model.

## Method

```
                        SkillRouter (Qwen2.5-7B)
                                |
              +-----------------+-----------------+
              |                                   |
         Lazy mode                           Plan mode
   <final_answer>answer</final_answer>    <plan round="1">
                                            <subtask id="1" depends_on="">identify bug location</subtask>
                                            <subtask id="2" depends_on="1">generate fix</subtask>
                                          </plan>
                                          <route model="gpt-5.3-codex" skill="read_code">...</route>
                                          <route model="claude-opus-4-6" skill="execute_python">...</route>
                                                |
                                                v
                                       9 worker models x 13 skills
                                       (real API calls at inference)
                                                |
                                                v
                                          <obs subtask="1">bug is in line 42...</obs>
                                          <obs subtask="2">patched version: ...</obs>
                                          <verify round="1" status="pass">both subtasks correct</verify>
                                          <final_answer>...</final_answer>
```

### Worker Pool

9 models across 5 families, 13 skills. Cost ranges from $1.25/M tokens (haiku) to $75/M tokens (opus). The router learns to balance accuracy vs cost -- picking cheap models for easy subtasks and expensive models only when needed. Full definition in `configs/pools.yaml`.

### Training Pipeline

```
HuggingFace datasets (31 sources, 9 domains)
    |
    v
[Phase C] Teacher distillation --> 58,457 validated trajectories
    |
    v
[Phase D] SFT warm-start (3 epochs, lr=2e-5, 4xH100) --> schema + routing learned
    |
    v
[Phase E] GiGPO RL (100 steps, 7xH100) --> cost-aware delegation boundary optimized
    |
    v
[Phase F] Evaluation (8 baselines x 17 benchmarks, official Docker verification)
```

## Evaluation

Unified eval pipeline supporting any router on any benchmark:

```bash
python -m eval_pipeline.run --router ROUTER --bench BENCH --api_key KEY

# Routers: router-r1, skillrouter-sft, direct, random,
#          oracle-cheapest, oracle-strongest, oracle-codex
# Benchmarks: swebench (500 instances), terminalbench (89 tasks)
```

Verification uses official methods:
- **SWE-bench**: `swebench.harness.run_evaluation` (Docker apply + test suite)
- **Terminal-Bench**: Harbor Docker (container per task, `test.sh` verification)

### Baselines

| System | Type | Description |
|--------|------|-------------|
| Direct(Qwen2.5-7B) | No routing | Base policy model, no delegation |
| Direct(GPT-5.4) | No routing | Strongest single model upper bound |
| Oracle-Cheapest | Fixed routing | Always pick cheapest (haiku) |
| Oracle-Strongest | Fixed routing | Always pick most expensive (opus) |
| Oracle-Codex | Fixed routing | Always pick code specialist |
| Random | Random routing | Random model from pool |
| Router-R1 | Learned routing | External baseline (3B, no decomposition) |
| **SkillRouter-SFT** | **Learned routing + decomposition** | Our method (SFT only) |
| **SkillRouter-RL** | **Learned routing + decomposition + RL** | Our full method |

### Current Progress

All generation complete. Docker verification in progress.

| Baseline | SWE-bench (500) | Terminal-Bench (89) |
|----------|:---:|:---:|
| Router-R1 | 500 gen, 500 verified | 500 gen, 30/89 verified |
| SkillRouter-SFT | 500 gen | 89 gen, 17/89 verified |
| Direct(Qwen2.5-7B) | 500 gen | 89 gen, 19/89 verified |
| Direct(GPT-5.4) | 500 gen, 500 verified | 89 gen, 25/89 verified |
| Oracle-Codex | 500 gen, 500 verified | 89 gen, 16/89 verified |
| Oracle-Strongest | 500 gen, 500 verified | 89 gen, 27/89 verified |
| Oracle-Cheapest | 500 gen, 500 verified | 89 gen, 29/89 verified |
| Random | 500 gen, 500 verified | 89 gen, 29/89 verified |

## Repository Structure

```
multiagentRL/
  configs/
    pools.yaml                 # Worker pool: 9 models x 13 skills
    sft/                       # SFT training configs
  docs/
    experiment_plan.md         # Full experiment plan
    schema.md                  # Trajectory schema v1.1
    pipeline.md                # Data + training pipeline
    eval_status.md             # Evaluation progress
  scripts/
    data/                      # Teacher distillation scripts
    sft/                       # SFT training scripts
    rl/                        # GiGPO RL training scripts
  eval_pipeline/               # Unified evaluation framework
    config.py                  # Model pool, costs, skills
    run.py                     # Main entry point
    routers/                   # Router adapters
    benchmarks/                # Benchmark adapters (swebench, terminalbench)
  agent_system/
    environments/              # RL environment with real API sub-agents
```

## Quick Start

```bash
# Evaluation
python -m eval_pipeline.run --router skillrouter-sft --bench swebench \
    --local_base http://localhost:8000/v1 --local_model SkillRouter-SFT --api_key KEY

# Training
python scripts/data/generate_trajectories.py --full --concurrency 200  # Distillation
bash scripts/sft/run_sft.sh                                           # SFT
bash scripts/rl/run_gigpo_skillrouter.sh                               # RL
```

See `docs/experiment_plan.md` for the complete experiment design.
