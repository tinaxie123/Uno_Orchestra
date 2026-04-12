# Experiment Plan v4 (Supersedes v3)

**Target**: NeurIPS 2026

---

## 0. Hard Rule — Data Contamination Policy

**Any benchmark family used in evaluation MUST NOT appear in any training stage** — neither SFT, nor RL, nor distillation prompts.

- SFT data, RL prompt pool, and eval benchmarks have ZERO overlap (verified at source level)
- 13-gram decontamination run after every distillation batch
- RL train/val split at SOURCE level (no source appears in both)

---

## 1. Data Split

### 1.1 SFT Training Data (Phase C, DONE)

58,457 distilled trajectories across 9 domains, schema v1.1 validated.

Sources: HotpotQA, 2WikiMultihopQA, MuSiQue, StrategyQA, NQ Open, TriviaQA, WebQuestions, GSM8K, MATH, TheoremQA, AQuA-RAT, APPS, CodeContests, SciQ, ARC-Challenge, OpenBookQA, MMLU (STEM), CommonsenseQA, PIQA, Social IQA, Winogrande, LogiQA 2.0, FOLIO, BBH, QuALITY, LegalBench, FinQA, ToolACE.

### 1.2 RL Training Data (Phase E, IN PROGRESS)

12,000 samples from sources NOT in SFT. Split by source:

| Domain | Source | Train | Val (held-out source) |
|--------|--------|-------|-----------------------|
| QA/Reasoning | DROP | — | 2,835 (val only) |
| Math (competition) | DAPO (Open-AgentRL) | 2,854 | — |
| Math (mixed) | NuminaMath-CoT (excl. gsm8k/math) | 639 | numinamath_cn_k12: 516 (val only) |
| Code | TACO + LeetCode-Easy/Hard | 2,505 | LeetCode-Medium: 71 (val only) |
| Science | mega-science (Open-AgentRL) | 1,903 | — |
| **Total** | | **7,901** | **3,422** |

Train/val source overlap: **0**.

### 1.3 Evaluation Benchmarks (ALL held out)

**Primary** (main table):

| Benchmark | Tests |
|-----------|-------|
| GAIA | Long-horizon multi-tool reasoning |
| BrowseComp-Plus | Deep-research with fixed retrieval |
| WideSearch | Parallel decomposition |
| ToolBench | Tool routing and function selection |
| Terminal-Bench 2.0 | Execution-heavy coding |

**Secondary**: DeepResearch Bench, Toolathlon, MRCR v2, LiveCodeBench, SWE-bench.

**Supporting**: AIME, AMC, GSM-Hard, GPQA, MMLU, MBPP, HumanEval.

---

## 2. Model & Pools

**Policy model**: Qwen2.5-7B-Instruct (SFT warm-start → RL fine-tune)

**Worker pool**: 9 executor models across 5 families (`configs/pools.yaml`)

| Tier | Model | USD/1M output tokens |
|------|-------|---------------------|
| nano | claude-haiku-4-5-20251001 | $1.25 |
| nano | gemini-2.5-flash | $1.50 |
| mid | kimi-k2.5 | $2.00 |
| mid | claude-sonnet-4-6 | $15.00 |
| mid | gemini-3.1-pro-preview | $10.00 |
| code | gpt-5.3-codex | $20.00 |
| large | qwen3.6-plus | $8.00 |
| large | claude-opus-4-6 | $75.00 |
| large | gpt-5.4 | $60.00 |

**Skill pool**: 13 skills: direct_answer, reason, web_search, database_query, read_document, read_code, extract_field, parse_structured, symbolic_math, execute_python, execute_shell, fact_check, call_api.

---

## 3. Phase D — SFT Warm-Start (DONE)

**Config**: `configs/sft/router_sft_qwen25_7b.yaml`

| Parameter | Value |
|-----------|-------|
| Base model | Qwen2.5-7B-Instruct |
| Framework | LlamaFactory |
| GPUs | 4× H100 80GB |
| Epochs | 3 |
| Effective batch | 128 (1 × 32 × 4) |
| cutoff_len | 8192 (zero truncation) |
| packing | true |
| Learning rate | 2e-5, cosine, warmup 67 steps |
| DeepSpeed | ZeRO-2 |
| Duration | 6h 51m |

**Results**:

| Metric | Value |
|--------|-------|
| train_loss | 0.2506 |
| eval_loss | 0.2976 |
| Format correctness | 100% (all 5 test queries produce valid schema v1.1) |
| Skill routing | Correct (math→symbolic_math, factual→web_search, code→execute_python) |
| Model selection | Correct (easy→haiku, hard→opus, code→codex) |
| DAG construction | Correct (depends_on, parallel subtasks) |

**Checkpoint**: `/home/xieht/data/sft/checkpoints/router_qwen25_7b_full_sft`

---

## 4. Phase E — RL Fine-Tuning (IN PROGRESS)

### 4.1 Algorithm: GiGPO

Two-level advantage decomposition (maps to HCPO design):

| Level | GiGPO concept | SkillRouter mapping |
|-------|---------------|---------------------|
| Episode-level | Group relative advantage across N rollouts of same query | A_conductor: "Is this decomposition plan good?" |
| Step-level | Anchor-based grouping across trajectories at same state | A_agent: "Is this (model, skill) choice good for this subtask?" |

Joint advantage: `score = episode_advantage + w × step_advantage`

**Anchor**: `<obs>` content returned by sub-agents. Trajectories reaching same observation state get grouped for step-level advantage normalization.

### 4.2 Reward Function (Router-R1 style)

```
R = (1 - α) × R_outcome + α × R_cost

R_outcome = correctness(final_answer, gold)    ∈ {0, 1}
R_cost    = 1 - total_api_cost / MAX_COST      ∈ [0, 1]

total_api_cost = Σ m(model_i) × T_output_i / 1M
```

- `m(model)`: per-token cost from real API pricing table
- `α = 0.1` (default): 90% weight on correctness, 10% on cost efficiency
- Hierarchical: if format invalid → R = 0 (invalid action penalty)

### 4.3 Environment: Real API Sub-Agents

Each `<route>` call triggers a **real API call** to qwen-plus via DashScope:
- Skill-specific system prompt (e.g., "You are a math solver" for symbolic_math)
- No chain-of-thought, direct answer only
- max_tokens=256, temperature=0.3
- Cost computed using the ROUTED model's pricing (not qwen-plus actual cost)

This creates real routing signal: different skills produce different quality responses for different query types.

### 4.4 Training Config

| Parameter | Value |
|-----------|-------|
| Framework | verl-agent (GiGPO) |
| GPUs | 7× H100 80GB |
| Base model | SFT checkpoint |
| train_batch_size | 112 |
| rollout.n | 8 (rollouts per query) |
| max_steps | 3 (multi-turn rounds) |
| total_training_steps | 100 |
| Actor LR | 1e-6 |
| KL loss | 0.001 (low_var_kl) |
| gpu_memory_utilization | 0.9 |
| Sub-agent API | DashScope qwen-plus |

### 4.5 DAG Quality in Reward

The reward implicitly captures DAG quality through outcome:
- Good DAG (correct dependencies, right granularity) → sub-agents succeed → correct final_answer → R_outcome=1
- Bad DAG (missing dependencies, over-decomposition) → sub-agents confused → wrong answer → R_outcome=0
- Redundant subtasks → higher cost with no accuracy gain → R_cost penalty

No explicit DAG structure reward needed — the outcome-based reward naturally selects for good decomposition via the pipeline: plan quality → route quality → obs quality → answer quality.

---

## 5. Phase F — Evaluation (PLANNED)

### 5.1 Metrics

| Metric | Description |
|--------|-------------|
| **Accuracy** | Exact match / F1 on held-out benchmarks |
| **Cost** | Total API cost per query (USD) |
| **Pareto efficiency** | Accuracy vs Cost frontier |
| **Routing diversity** | Entropy of (model, skill) pair distribution |
| **Lazy rate** | % of queries answered without decomposition |
| **Repair rate** | % of trajectories using verify→repair loop |
| **DAG depth** | Average subtask dependency chain length |

### 5.2 Baselines

| System | Type |
|--------|------|
| Direct prompting (Qwen2.5-7B) | No routing, single model |
| SFT-only (our Phase D) | Format learned, no RL optimization |
| GPT-5.4 direct | Strong single-model baseline |
| Router-R1 | RL-trained model router (no decomposition) |
| WideSeek-R1 | MARL decomposition (homogeneous agents) |

### 5.3 Evaluation Protocol

1. Load RL checkpoint
2. For each benchmark query:
   - Generate trajectory with greedy decoding (temperature=0)
   - Execute routes via real API calls
   - Compute correctness + cost
3. Report: accuracy, cost, Pareto curve, per-domain breakdown

---

## 6. Phase G — Ablation Studies (PLANNED)

Required for NeurIPS. Each ablation changes ONE variable, keeping all else fixed.

### 6.1 SFT-only vs SFT+RL

| Experiment | Config | Purpose |
|------------|--------|---------|
| SFT-only | Phase D checkpoint, no RL | Baseline: what SFT alone achieves |
| SFT+RL | Phase E checkpoint | Full method: incremental gain from RL |

### 6.2 Algorithm Comparison

| Experiment | `algorithm.adv_estimator` | Purpose |
|------------|--------------------------|---------|
| GiGPO (ours) | `gigpo` | Two-level advantage (episode + step) |
| GRPO | `grpo` | Episode-level only (no step grouping) |
| PPO + GAE | `gae` | Standard PPO baseline |

**Key claim**: GiGPO's step-level advantage gives better credit assignment for routing decisions than episode-level-only methods.

### 6.3 Cost Weight α Sweep

| α | Expected behavior |
|---|-------------------|
| 0.0 | Pure accuracy, no cost optimization |
| 0.05 | Slight cost awareness |
| 0.1 | Default (current) |
| 0.2 | Strong cost pressure |
| 0.5 | Heavy cost optimization, accuracy may drop |

**Output**: Pareto frontier plot (accuracy vs average cost per query).

### 6.4 Group Size N

| `env.rollout.n` | Samples per query | Purpose |
|-----------------|-------------------|---------|
| 2 | Minimum for group advantage | Baseline exploration |
| 4 | Moderate | Balance |
| 8 | Default | Current setting |
| 16 | Maximum | Diminishing returns? |

### 6.5 Pool Size Ablation

| Experiment | Models | Skills | Purpose |
|------------|--------|--------|---------|
| Minimal | 2 (haiku, opus) | 3 (direct, search, math) | Does routing matter with few options? |
| Medium | 5 | 6 | Sweet spot? |
| Full | 9 | 13 | Current setting |

**Key claim**: Larger pools benefit more from learned routing than smaller pools.

---

## 7. Implementation Status

| Phase | Task | Status | Artifact |
|-------|------|--------|----------|
| A | Schema v1.1 | ✅ DONE | `docs/schema_v1_1.md` |
| B | Pilot distillation | ✅ DONE | — |
| C | Full distillation (58,457) | ✅ DONE | `train_final.parquet` |
| D | SFT warm-start | ✅ DONE | `checkpoints/router_qwen25_7b_full_sft` |
| E | RL (GiGPO, 100 steps) | 🔄 IN PROGRESS | wandb: `skillrouter-rl` |
| F | Evaluation | ⬜ PLANNED | — |
| G | Ablation studies | ⬜ PLANNED | — |

---

## 8. Compute Budget

| Phase | GPUs | Duration | API Cost |
|-------|------|----------|----------|
| D (SFT) | 4× H100 | 7h | — |
| E (RL, 100 steps) | 7× H100 | ~8h | ~¥360 (DashScope) |
| F (Eval) | 1× H100 | ~2h | ~¥200 |
| G (Ablations, 5 runs) | 7× H100 | ~40h | ~¥1,800 |
| **Total** | | ~57h | ~¥2,360 |

---

## 9. Repository Structure

```
multiagentRL/
├── docs/
│   ├── experiment_plan_v3.md          ← THIS FILE
│   ├── schema_v1_1.md
│   ├── pipeline.md
│   └── project_intro.md
├── configs/
│   ├── pools.yaml
│   └── sft/
│       ├── router_sft_qwen25_7b.yaml
│       ├── ds_z2_config.json
│       └── dataset_info_entry.json
├── scripts/
│   ├── sft/
│   │   ├── prepare_data.py
│   │   └── run_sft.sh
│   ├── rl/
│   │   ├── prepare_prompt_pool.py
│   │   └── run_gigpo_skillrouter.sh
│   └── data/
│       ├── generate_trajectories.py
│       ├── validate_schema.py
│       └── build_dataset.py
├── agent_system/environments/env_package/skillrouter/
│   ├── envs.py              # Real API sub-agent environment
│   ├── env_manager.py       # verl-agent interface
│   └── projection.py        # Schema validation
└── data/
    └── sft_recipe.yaml
```
