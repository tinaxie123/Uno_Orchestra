# 

✅**Failure-Driven in context learning**
For each failed trajectory, we feed the full execution trace—including the Orchestrator's delegation decisions, sub-agent responses, and the final erroneous answer into GPT-4o, which diagnoses the root cause and assigns it to one of the following failure categories: (i) information loss which happens when the Orchestrator omits critical
context when delegating subtasks; (ii) premature aggregation—intermediate results are returned without completing the final computation; (iii) format mismatch—the answer is semantically correct but does not conform to the expected output format; and (iv) delegation scope error—the task is under or over decomposed.  Once failures are categorized, we generate a minimal, targeted constraint for each high-frequency category and inject it into the Orchestrator's instruction. Crucially, these patches are not instance-specific fixes tied to particular failing examples; rather, they clarify the Orchestrator's general understanding of the task protocol—such as what constitutes a complete answer or what information must be preserved during delegation. The resulting constraints are task-agnostic and transfer to unseen problems, since they address systematic gaps in how the Orchestrator interprets its role rather than
surface-level errors on individual inputs. 

📷This diagnostic-then-patch loop runs for 3 rounds. By the third round, the failure taxonomy reveals that all remaining errors stem from suboptimal routing decisions—such as dispatching a complex symbolic reasoning task to a lightweight model—rather than ambiguity in the Orchestrator's instructions. This indicates that prompt clarity has been saturated, and further gains   require improving the Router's model selection policy.

## Error taxonomy




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

9 models across 4 providers (no Qwen in sub-agent pool), 13 skills. Output cost ranges from $0.40/M (gemini-2.5-flash-lite) to $25/M (claude-opus-4.6). The router learns to balance accuracy vs cost -- picking cheap models for easy subtasks and expensive models only when needed. Full definition in `configs/pools.yaml`.

### Training Pipeline



### Parameters

max token length 

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
| |  |  |
| GPT-5.4 | No routing | Strongest single model upper bound |
| Cheapest | Fixed routing | Always pick cheapest (haiku) |

| Strongest | Fixed routing | Always pick most expensive (opus) |
| **SkillRouter-Base**| | Random model from pool |
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
