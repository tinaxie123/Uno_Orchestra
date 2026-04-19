### 🍬Data Source Pool Construction 

**🍰 Data Source pool construction.** 

A router must learn **when** and **how** to decompose a task. According to the capability taxonomy of general AI assistants (Mialon et al., 2024), we organize our data along four dimensions reflecting different decomposition patterns: 

🍓**reasoning**, where a problem must be broken into a chain of inferential or computational steps;  

🥭**knowledge retrieval**, where the router issues parallel or sequential queries to gather information from multiple sources; 

🍊**tool use**, where sub-tasks involve heterogeneous operations such as code execution or API calls;

🍑**multi-step planning**, where intermediate results shape subsequent actions.   


We select a minimal set of sources such that each of the four dimensions is covered by at least one dataset, and every source contributes a decomposition pattern not provided by the others. Starting from an initial pool of 8 widely-used datasets spanning 4 domains, we apply two inclusion criteria to ensure each source provides meaningful signal for router training:
(i) the task must exercise the router's decision-making capability, spanning both single-step tasks where the router learns to dispatch directly to an appropriate model, and multi-step tasks where it must decompose the problem into dependent sub-tasks;
(ii) gold answers must be automatically verifiable to enable scalable filtering;

Sources:

- GSM8K (Cobbe et al., 2021) — grade-school math, teaches the router when NOT to decompose
- DAPO-Math-17k (Yu et al., 2025) and NuminaMath-CoT (Li et al., 2024) — competition-level mathematical reasoning
- DROP, train split (Dua et al., 2019) — span extraction and arithmetic over paragraphs
- HotpotQA (Yang et al., 2018), train split — 2-hop question answering
- MuSiQue (Trivedi et al., 2022), train split — 3-4 hop question answering requiring deeper decomposition
- TACO (Li et al., 2023) — competitive programming
- ToolACE (Liu et al., 2024) — multi-step tool orchestration involving API chaining and sequential planning

**🧁 Stratified coverage sampling.** We construct the training pool by drawing a fixed quota from each source so that four orthogonal capability axes are each exercised by at least two datasets and no single axis dominates the mixture. This yields approximately 10k raw tasks. After bootstrapped curriculum filtering , 


| Capability axis | What the router must learn | Datasets |
|---|---|---|---|
| **Atomic reasoning** |  forward the task to a single model | GSM8K | 500 |
| **Compositional reasoning** | Multi-step symbolic manipulation requiring chain-of-thought delegation | NuminaMath-CoT | 
| **Knowledge retrieval**  | Decompose into independent evidence-gathering subtasks | DROP, HotpotQA | 
| **Knowledge composition** | Deep sequential decomposition with inter-subtask dependencies | MuSiQue | 
| **Tool orchestration** | Select correct tool–model pairs and chain API calls | TACO, ToolACE | 

### 🍭Data Selection Pipeline

We emply **bootstrapped curriculum filtering** on raw question sets from training data pools for supervised fine-tuning and reinforcement learning to ensure the training set consists entirely of router's capability gaps. 

### 🍒Bootstrapped curriculum filtering

*Stage 1: Router probe.* We run the current router checkpoint on every task in the pool with real sub-model execution — the router decomposes the task, delegates sub-tasks to actual models, and produces a final answer. We evaluate each task via pass\@3 and check against the gold label. Tasks the router already solves correctly are discarded, as they carry no learning signal.

*Stage 2: Teacher trajectory collection and SFT/RL split.* For each remaining task — where the router failed — we run a strong teacher orchestrator with the same model pool. If the teacher produces a correct trajectory, the task enters the SFT set as a demonstration for imitation learning. If the teacher also fails, the task enters the RL set, where the router must discover a working decomposition through its own exploration.

*Stage 3: Overlong filtering.* Following the overlong filtering strategy of DAPO (Yu et al., 2025), we discard any trajectory whose token count exceeds the training context length. Truncated trajectories teach the model to produce incomplete decompositions; removing them allows the model to generalize to longer reasoning chains at inference time without incurring penalties from truncation during training.

This pipeline is self-adaptive: it can be re-applied after each training round to produce a curriculum of increasing difficulty, as the router's capability boundary shifts with training.


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
