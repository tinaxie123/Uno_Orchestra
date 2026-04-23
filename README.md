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

*Stage 1*:  **Router probe**. We run the current router checkpoint on every task in the pool with real sub-model execution — the router decomposes the task, delegates sub-tasks to actual models, and produces a final answer. We evaluate each task via pass\@3 and check against the gold label. Tasks the router already solves correctly are discarded, as they carry no learning signal.

*Stage 2*: **Teacher trajectory**  For each remaining task — where the router failed — we run a strong teacher orchestrator with the same model pool. If the teacher produces a correct trajectory, the task enters the SFT set as a demonstration for imitation learning. If the teacher also fails, the task enters the RL set, where the router must discover a working decomposition through its own exploration.

*Stage 3*: **Noise removal**: trajectories with infrastructure artifacts (API timeouts, incomplete responses) or dataset annotation errors (gold answers that are not valid API calls) are discarded, as they provide neither correct demonstrations nor meaningful reward signal.

This pipeline is self-adaptive: it can be re-applied after each training round to produce a curriculum of increasing difficulty, as the router's capability boundary shifts with training.


✅**Failure-Driven in context learning**
For each failed trajectory, we feed the full execution trace—including the Orchestrator's delegation decisions, sub-agent responses, and the final erroneous answer into GPT-4o, which diagnoses the root cause and assigns it to one of the following failure categories: (i) information loss which happens when the Orchestrator omits critical
context when delegating subtasks; (ii) premature aggregation—intermediate results are returned without completing the final computation; (iii) format mismatch—the answer is semantically correct but does not conform to the expected output format; and (iv) delegation scope error—the task is under or over decomposed.  Once failures are categorized, we generate a minimal, targeted constraint for each high-frequency category and inject it into the Orchestrator's instruction. Crucially, these patches are not instance-specific fixes tied to particular failing examples; rather, they clarify the Orchestrator's general understanding of the task protocol—such as what constitutes a complete answer or what information must be preserved during delegation. The resulting constraints are task-agnostic and transfer to unseen problems, since they address systematic gaps in how the Orchestrator interprets its role rather than
surface-level errors on individual inputs. 

📷This diagnostic-then-patch loop runs for 3 rounds. By the third round, the failure taxonomy reveals that all remaining errors stem from suboptimal routing decisions—such as dispatching a complex symbolic reasoning task to a lightweight model—rather than ambiguity in the Orchestrator's instructions. This indicates that prompt clarity has been saturated, and further gains   require improving the Router's model selection policy.

## Dataset Description

Of 12,803 sampled tasks, 5,589 (43.7%) are already solved by the current router and discarded. 7,214 tasks survive to the teacher stage: 3,174 yield successful SFT demonstrations and 4,549 enter the initial RL pool. After overlong filtering (> 8,192 tokens), the base SFT set contains 2,762 trajectories.

**Augmentation & Rescue.** We apply two additional passes to expand SFT and shrink the RL pool:

1. **Rejection-sampled augmentation** (`scripts/data/augment_sft.py`): for each SFT task we draw K=2 additional teacher rollouts at temperatures {0.5, 1.0}; hard (RL-pool) tasks receive K=3 at {0.3, 0.7, 1.0}. Only trajectories that pass the per-source verifier are kept. This adds diverse demonstrations for the same questions.

2. **RL-pool rescue** (`scripts/data/rescue_rl_pool.py`): for each task in the RL pool (where the original teacher failed), we retry with a stronger teacher cascade (gemini-2.5-pro, then claude-sonnet-4-6, then gpt-5.4) with pass@3. Tasks that any stronger teacher solves are promoted from RL to SFT.

After augmentation and rescue, the final dataset is:

| Capability Axis         | Benchmarks     |    Sampled |       Router OK |       SFT |   RL Pool |
| ----------------------- | -------------- | ---------: | --------------: | --------: | --------: |
| Atomic reasoning        | GSM8K          |        500 |     483 (96.6%) |        40 |        15 |
| Compositional reasoning | NuminaMath     |      1,793 |   1,191 (66.4%) |       282 |       355 |
| Knowledge retrieval     | DROP, HotpotQA |      3,808 |   2,466 (64.8%) |       554 |       551 |
| Knowledge composition   | MuSiQue        |      1,746 |     739 (42.3%) |       196 |       773 |
| Tool orchestration      | TACO, ToolACE  |      4,956 |     710 (14.3%) |     1,985 |     1,282 |
| **Total**               |                | **12,803** | **5,589 (43.7%)** | **3,057** | **2,976** |

The rescue pass reduced the RL pool from 4,549 to **2,976** tasks (−34.6%), converting 295 previously unsolvable tasks into SFT demonstrations. The strongest gains came from tool orchestration, where gemini-2.5-pro's superior code generation solved many TACO tasks that the original qwen3.5-plus teacher could not.

Tool orchestration receives the largest share of SFT demonstrations (64.9%) because routing decisions in this axis involve both model selection *and* skill selection — the router must learn to match coding tasks to code-capable models and API-calling tasks to tool-aware models, requiring more diverse demonstrations than axes where only model selection matters. Atomic reasoning receives the smallest share (1.3%) because the router already solves 96.6% of these tasks; the remaining SFT examples serve primarily as a negative signal, teaching the router to recognize single-step tasks that should *not* be decomposed into subtasks.

The high router-OK rate for atomic reasoning (96.6%) and knowledge retrieval (64.8%) confirms that a 7B-parameter policy can already handle single-hop factual and arithmetic tasks through direct answering. In contrast, the near-zero router-OK rate for tool orchestration (14.3%) validates our design choice to treat tool selection as a learned routing problem rather than a fixed heuristic — the current router cannot solve these tasks without training on delegation trajectories.

## overview of sft dataset

dalegation


## Error Taxonomy

Base router

The Qwen2.5-7B router solves 43.7% of the 12,803 sampled tasks under pass@3; the remaining 7,214 tasks yield 21,642 failing rollouts that we classify by root cause. Success varies sharply across capability axes (atomic reasoning 96.6%, compositional 66.4%, knowledge retrieval 64.8%, knowledge composition 42.3%, tool orchestration 14.3%). Roughly three-quarters of failures are content errors and the remaining quarter are protocol errors, all observed under the same source-aware planner prompt the teacher uses (so the failures reflect capability gaps, not prompting choices). The content failures concentrate on three delegation patterns: *output-not-code* on competitive programming (34.2% of all failures — TACO), where the router answers in prose despite having a code-generation specialist in its pool; *wrong-entity* errors on multi-hop QA (22.6% — HotpotQA/MuSiQue), where the router cannot maintain coherent reasoning chains across hops; and *natural-language instead of tool call* on ToolACE (2.1%), where the router produces an English description instead of emitting the call even though the tool schema is present in its context. Protocol failures are heavily skewed toward tool use: >80% of ToolACE failures either never issue a tool call (26%) or never reach a `finish()` within the step budget (59%). SFT is designed to close the protocol gap by imitating teacher trajectories; RL on the 4,549-task residual pool then optimizes the content-level routing decisions that remain after the shape is fixed.

## Model comparison

We also evaluate a smaller router (Qwen3-4B) on the same pipeline. Its failure profile differs qualitatively:

| Failure Mode                         | Qwen2.5-7B (round1) | Qwen3-4B (round1\_q3) |
| ------------------------------------ | ------------------: | --------------------: |
| Protocol failure (no finish / empty) |               24.3% |                 98.1% |
| Wrong answer                         |               74.0% |                  1.9% |

The 7B router's failures are dominated by *capability* limitations (wrong answers), while the 4B router fails almost exclusively at *protocol compliance* (unable to produce valid tool calls or finish actions). This suggests that protocol-following ability is a prerequisite that emerges between 4B and 7B scale, and that RL training for the 4B model should prioritize format compliance before routing quality.


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
#          oracle-cheapest, router+claude, oracle-codex
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
| router+claude | 500 gen, 500 verified | 89 gen, 27/89 verified |
| Oracle-Cheapest | 500 gen, 500 verified | 89 gen, 29/89 verified |
| Random | 500 gen, 500 verified | 89 gen, 29/89 verified |

## Repository Structure

```
multiagentRL/
  configs/
    pools.yaml                 # Worker pool: 10 models x 13 skills
    sft/                       # SFT training configs
  docs/
    pipeline.md                # Data + training pipeline
    error taxonomy.md          # Teacher/router failure taxonomy
    case_studies/              # Worked examples (fix-git, subtask-conflict)
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

See `docs/pipeline.md` for the data + training pipeline overview.
