# Experiment Plan v5 (Supersedes v4)

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
| SWE-bench Verified | Repository-level bug fixing (500 instances) |
| Terminal-Bench 2.0 | Execution-heavy coding (89 tasks) |
| GAIA | Long-horizon multi-tool reasoning |
| BrowseComp-Plus | Deep-research with fixed retrieval |
| ToolBench | Tool routing and function selection |

**Secondary**: DeepResearch Bench, Toolathlon, MRCR v2, LiveCodeBench, WideSearch.

**Supporting**: AIME, AMC, GSM-Hard, GPQA, MMLU, MBPP, HumanEval.

---

## 2. Model & Pools

**Policy model**: Qwen2.5-7B-Instruct (SFT warm-start -> RL fine-tune)

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
| GPUs | 4x H100 80GB |
| Epochs | 3 |
| Effective batch | 128 (1 x 32 x 4) |
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
| Skill routing | Correct (math->symbolic_math, factual->web_search, code->execute_python) |
| Model selection | Correct (easy->haiku, hard->opus, code->codex) |
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

Joint advantage: `score = episode_advantage + w x step_advantage`

### 4.2 Reward Function

```
R = (1 - alpha) x R_outcome + alpha x R_cost

R_outcome = correctness(final_answer, gold)    in {0, 1}
R_cost    = 1 - total_api_cost / MAX_COST      in [0, 1]

total_api_cost = SUM m(model_i) x T_output_i / 1M
```

- `m(model)`: per-token cost from real API pricing table
- `alpha = 0.1` (default): 90% weight on correctness, 10% on cost efficiency
- Hierarchical: if format invalid -> R = 0 (invalid action penalty)

### 4.3 Environment: Real API Sub-Agents

Each `<route>` call triggers a **real API call** to qwen-plus via DashScope:
- Skill-specific system prompt (e.g., "You are a math solver" for symbolic_math)
- No chain-of-thought, direct answer only
- max_tokens=256, temperature=0.3
- Cost computed using the ROUTED model's pricing (not qwen-plus actual cost)

### 4.4 Training Config

| Parameter | Value |
|-----------|-------|
| Framework | verl-agent (GiGPO) |
| GPUs | 7x H100 80GB |
| Base model | SFT checkpoint |
| train_batch_size | 112 |
| rollout.n | 8 (rollouts per query) |
| max_steps | 3 (multi-turn rounds) |
| total_training_steps | 100 |
| Actor LR | 1e-6 |
| KL loss | 0.001 (low_var_kl) |
| gpu_memory_utilization | 0.9 |
| Sub-agent API | DashScope qwen-plus |

---

## 5. Phase F — Evaluation (IN PROGRESS)

### 5.1 Unified Evaluation Pipeline

Built a modular eval framework at `eval_pipeline/` supporting any router x any benchmark combination.

```
eval_pipeline/
  config.py               # Single source: 9-model pool, cost table, 13 skills
  run.py                  # Entry: python -m eval_pipeline.run --router X --bench Y
  routers/
    base.py               # BaseRouter interface -> RouteResult
    router_r1.py          # Router-R1 (3B, <think>-><search>-><answer>)
    skillrouter_sft.py    # SkillRouter SFT (7B, <plan>-><route>-><obs>-><final_answer>)
    direct.py             # Single model, no routing
    random_router.py      # Random model selection
    oracle.py             # Fixed model (cheapest/strongest/codex)
  benchmarks/
    swebench.py           # SWE-bench Verified (official swebench harness Docker eval)
    terminalbench.py      # Terminal-Bench 2.0 (Harbor Docker, test.sh verification)
```

**Key design principles**:
- All routers share the same model pool and cost table from `config.py`
- Verification uses official methods: swebench harness (batch Docker) for SWE-bench, Harbor Docker containers for Terminal-Bench
- Pipeline mode for Terminal-Bench: generate + Docker verify concurrently
- Resume support: cached predictions and verification results survive restarts

### 5.2 Metrics

| Metric | Description |
|--------|-------------|
| **Accuracy** | Resolved rate (SWE-bench) / Pass rate (Terminal-Bench) / EM/F1 (QA) |
| **Cost** | Total API cost per query (USD) |
| **Pareto efficiency** | Accuracy vs Cost frontier |
| **Routing diversity** | Entropy of (model, skill) pair distribution |
| **Lazy rate** | % of queries answered without decomposition |
| **Repair rate** | % of trajectories using verify->repair loop |
| **DAG depth** | Average subtask dependency chain length |

### 5.3 Baselines

| # | System | Type | Model | Paper Role |
|---|--------|------|-------|------------|
| 1 | Direct(Qwen2.5-7B) | No routing | Base policy model | Routing adds value over base model |
| 2 | SkillRouter-SFT | Learned routing + decomposition | 7B SFT checkpoint | Ablation: SFT alone (no RL) |
| 3 | Direct(GPT-5.4) | No routing | Strongest single model | Upper bound: best model, no routing |
| 4 | Router-R1 | Learned routing, no decomposition | 3B pre-trained | External: model-only routing |
| 5 | Oracle-Codex | Fixed -> code specialist | GPT-5.3-Codex always | Is specialist always best for code? |
| 6 | Oracle-Strongest | Fixed -> strongest | Claude-Opus-4.6 always | Cost upper bound |
| 7 | Oracle-Cheapest | Fixed -> cheapest | Claude-Haiku-4.5 always | Quality lower bound |
| 8 | Random | Random model selection | Random from pool | Learned routing beats random? |
| 9 | WideSeek-R1 | MARL decomposition | Homogeneous agents | External: MARL baseline |

### 5.4 Evaluation Protocol

1. Load checkpoint (SFT or RL)
2. For each benchmark query:
   - Generate trajectory with greedy decoding (temperature=0 for router, 0.3 for sub-agents)
   - Execute routes via real API calls (xiaojingai endpoint)
   - Compute correctness + cost
3. Verify:
   - **SWE-bench**: official `swebench.harness.run_evaluation` (batch Docker apply + test)
   - **Terminal-Bench**: Harbor Docker container per task (execute solution -> run test.sh -> reward.txt)
4. Report: accuracy, cost, Pareto curve, per-domain breakdown, routing diversity

### 5.5 Current Evaluation Progress (as of 2026-04-13)

**Generation phase** (router -> predictions):

| Baseline | SWE-bench (500) | Terminal-Bench (89) |
|----------|:---:|:---:|
| Router-R1 | 493/500 | 89/89 DONE |
| SkillRouter-SFT | 38/500 (running GPU 2) | 18/89 (running GPU 2) |
| Direct(Qwen2.5-7B) | 123/500 (running GPU 3) | 79/89 (running GPU 3) |
| Direct(GPT-5.4) | 500/500 DONE | 89/89 DONE |
| Oracle-Codex | 500/500 DONE | 89/89 DONE |
| Oracle-Strongest | 500/500 DONE | 89/89 DONE |
| Oracle-Cheapest | 500/500 DONE | 89/89 DONE |
| Random | 286/500 (running) | 87/89 |

**Verification phase** (Docker execution -> real pass rate):
- SWE-bench: swebench harness invoked for 4 completed baselines
- Terminal-Bench: Pipeline mode active, 24 Docker containers running, 6-14 tasks verified per baseline

**Infrastructure**:
- GPU 2: vLLM serving SkillRouter-SFT (7B), port 8000
- GPU 3: vLLM serving Qwen2.5-7B-Instruct (base), port 8001
- Docker: 24+ containers active for Terminal-Bench verification
- API: 12 eval processes concurrent via xiaojingai

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

### 6.3 Cost Weight alpha Sweep

| alpha | Expected behavior |
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
| A | Schema v1.1 | DONE | `docs/schema_v1_1.md` |
| B | Pilot distillation | DONE | -- |
| C | Full distillation (58,457) | DONE | `train_final.parquet` |
| D | SFT warm-start | DONE | `checkpoints/router_qwen25_7b_full_sft` |
| E | RL (GiGPO, 100 steps) | IN PROGRESS | wandb: `skillrouter-rl` |
| F | Baseline evaluation | IN PROGRESS | `eval_pipeline/`, `eval_results/` |
| F.1 | Eval pipeline framework | DONE | `eval_pipeline/` (7 routers x 2 benchmarks) |
| F.2 | Generation (8 baselines) | 70% DONE | See section 5.5 |
| F.3 | SWE-bench Docker verification | IN PROGRESS | swebench harness running |
| F.4 | Terminal-Bench Docker verification | IN PROGRESS | 24 containers active |
| G | Ablation studies | PLANNED | -- |

---

## 8. Compute Budget

| Phase | GPUs | Duration | API Cost |
|-------|------|----------|----------|
| D (SFT) | 4x H100 | 7h | -- |
| E (RL, 100 steps) | 7x H100 | ~8h | ~360 CNY (DashScope) |
| F (Eval, all baselines) | 2x H100 | ~12h | ~500 CNY (xiaojingai) |
| G (Ablations, 5 runs) | 7x H100 | ~40h | ~1,800 CNY |
| **Total** | | ~67h | ~2,660 CNY |

---

## 9. Repository Structure

```
multiagentRL/
  docs/
    experiment_plan_v3.md          <- THIS FILE (v5)
    eval_status.md                 <- Live eval status report
    schema_v1_1.md
    pipeline.md
    project_intro.md
  configs/
    pools.yaml                     <- 9 models, 13 skills
    sft/
      router_sft_qwen25_7b.yaml
      ds_z2_config.json
      dataset_info_entry.json
  scripts/
    sft/
      prepare_data.py
      run_sft.sh
    rl/
      prepare_prompt_pool.py
      run_gigpo_skillrouter.sh
    data/
      generate_trajectories.py
      validate_schema.py
      build_dataset.py
  eval_pipeline/                   <- NEW: unified evaluation framework
    config.py                      <- Single source: model pool, costs
    run.py                         <- Main entry point
    routers/
      base.py                      <- BaseRouter interface
      router_r1.py                 <- Router-R1 adapter
      skillrouter_sft.py           <- SkillRouter SFT adapter
      direct.py                    <- Direct prompting (no routing)
      random_router.py             <- Random baseline
      oracle.py                    <- Fixed model baselines
    benchmarks/
      base.py                      <- BaseBenchmark interface
      swebench.py                  <- SWE-bench (official harness)
      terminalbench.py             <- Terminal-Bench (Harbor Docker)
  agent_system/environments/env_package/skillrouter/
    envs.py                        <- Real API sub-agent environment
    env_manager.py                 <- verl-agent interface
    projection.py                  <- Schema validation
  data/
    sft_recipe.yaml
```

---

## 10. Expected Paper Tables

### Table 1: Main Results (SWE-bench + Terminal-Bench)

| System | Type | SWE-bench Resolved | TB-2.0 Pass | Avg Cost/Query |
|--------|------|:---:|:---:|---:|
| Direct(Qwen2.5-7B) | No routing | -- | -- | -- |
| Direct(GPT-5.4) | No routing | -- | -- | $0.019 |
| Oracle-Cheapest | Fixed (haiku) | -- | -- | ~$0.00 |
| Oracle-Strongest | Fixed (opus) | -- | -- | $0.063 |
| Oracle-Codex | Fixed (codex) | -- | -- | $0.007 |
| Random | Random | -- | -- | -- |
| Router-R1 | Learned (no decomp) | -- | -- | ~$0.015 |
| **SkillRouter-SFT** | **Learned + decomp** | -- | -- | -- |
| **SkillRouter-RL** | **Learned + decomp + RL** | -- | -- | -- |

### Table 2: Routing Behavior Analysis

| Metric | Router-R1 | SkillRouter-SFT | SkillRouter-RL |
|--------|-----------|----------------|---------------|
| Avg routes/query | -- | -- | -- |
| Unique models used | -- | -- | -- |
| Skill diversity (entropy) | N/A | -- | -- |
| Lazy rate (no decomp) | -- | -- | -- |
| DAG depth | 0 | -- | -- |

### Figure: Pareto Frontier (Accuracy vs Cost)

Plot accuracy (y-axis) against average API cost per query (x-axis) for all baselines. SkillRouter-RL should dominate the Pareto frontier in the mid-cost region.
