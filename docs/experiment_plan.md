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

**🧁 Stratified coverage sampling.** We construct the training pool by drawing a fixed quota from each source so that four orthogonal capability axes are each exercised by at least two datasets and no single axis dominates the mixture:

| Capability axis | What the router must learn | Datasets | Quota |
|---|---|---|---|
| **Atomic reasoning** | When *not* to decompose — forward the task to a single model | GSM8K | 500 |
| **Compositional reasoning** | Multi-step symbolic manipulation requiring chain-of-thought delegation | NuminaMath-CoT | 1,500 |
| **Knowledge retrieval** (1–2 hop) | Decompose into independent evidence-gathering subtasks | DROP, HotpotQA | 1,500 each |
| **Knowledge composition** (3–4 hop) | Deep sequential decomposition with inter-subtask dependencies | MuSiQue | 1,500 |
| **Tool orchestration** | Select correct tool–model pairs and chain API calls | TACO, ToolACE | 1,750 each |

This yields ~10k raw tasks. After bootstrapped curriculum filtering (see below), approximately 25–35% survive into the SFT set and 10–18% into the RL set, with the remainder discarded as already-solved by the current router. The quota is deliberately **balanced across capability axes rather than across datasets**: tool orchestration receives the largest share (35%) because routing decisions in this axis involve both model selection *and* skill selection, requiring more diverse demonstrations. Atomic reasoning receives the smallest share (5%) because it serves purely as a negative signal — teaching the router to recognize tasks that should *not* be decomposed.

### 🍭Data Selection Pipeline

We emply **bootstrapped curriculum filtering** on raw question sets from training data pools for supervised fine-tuning and reinforcement learning to ensure the training set consists entirely of router's capability gaps. 

### 🍒Bootstrapped curriculum filtering

*Stage 1: Router probe.* We run the current router checkpoint on every task in the pool with real sub-model execution — the router decomposes the task, delegates sub-tasks to actual models, and produces a final answer. We evaluate each task via pass\@3 and check against the gold label. Tasks the router already solves correctly are discarded, as they carry no learning signal.

*Stage 2: Teacher trajectory collection and SFT/RL split.* For each remaining task — where the router failed — we run a strong teacher orchestrator with the same model pool. If the teacher produces a correct trajectory, the task enters the SFT set as a demonstration for imitation learning. If the teacher also fails, the task enters the RL set, where the router must discover a working decomposition through its own exploration.

*Stage 3: Overlong filtering.* Following the overlong filtering strategy of DAPO (Yu et al., 2025), we discard any trajectory whose token count exceeds the training context length. Truncated trajectories teach the model to produce incomplete decompositions; removing them allows the model to generalize to longer reasoning chains at inference time without incurring penalties from truncation during training.

This pipeline is self-adaptive: it can be re-applied after each training round to produce a curriculum of increasing difficulty, as the router's capability boundary shifts with training.


### 🧸SFT Training Data




### ☁️ RL Training Data

12,000 samples from sources NOT in SFT. Split by source:

| Domain | Source | Train | Val (held-out source) |
|--------|--------|-------|-----------------------| 
| QA/Reasoning | DROP MuSiQue   | -- | 2,835 (val only) |
| Math (competition) | DAPO (Open-AgentRL) | 2,854 | -- |
| Math (mixed) | NuminaMath-CoT (excl. gsm8k/math) | 639 | numinamath_cn_k12: 516 (val only) |
| Code | TACO + LeetCode-Easy/Hard | 2,505 | LeetCode-Medium: 71 (val only) |
| **Total** | | **7,901** | **3,422** |

Train/val source overlap: **0**.

### 🍦Evaluation Benchmarks

| Benchmark |
|-----------|--------|-----------|
| SWE-bench Verified | Repository-level bug fixing | 500 |
| Terminal-Bench 2.0 | Execution-heavy coding | 89 |
| GAIA | Long-horizon multi-tool reasoning | 165 |
| BrowseComp-Plus | Deep-research with fixed retrieval | -- |
| ToolBench | Tool routing and function selection | -- |
| WideSearch | Parallel decomposition | -- |
| DeepResearch Bench | Multi-step research | -- |

| MRCR v2 | Multi-round context reasoning | -- |
| LiveCodeBench v6| Code generation (live) | -- |
| AIME 2025| 
| MATH 500 | 
| DROP test | 
| GPQA | Graduate-level QA | 448 |
| MMLU | Multi-domain knowledge | 14042 |
| MBPP | Code generation | 500 |
| HumanEval | Code generation | 164 |

---

## 2. Model & Pools

**Policy model (compared)**: Qwen3-4B, Qwen2.5-7B-Instruct

**Worker pool**: 9 models across 5 providers (`configs/pools.yaml`)

| Model | USD/1M output tokens |
|-------|---------------------|
| gemini-2.5-flash | $0.60 |
| kimi-k2.5 | $2.50 |
| qwen3.6-plus | $3.00 |
| claude-haiku-4-5-20251001 | $4.00 |
| gemini-3.1-pro-preview | $12.00 |
| gpt-5.3-codex | $14.00 |
| gpt-5.4 | $15.00 |
| claude-sonnet-4-6 | $15.00 |
| claude-opus-4-6 | $75.00 |

**Skill pool**: 13 skills: direct_answer, reason, web_search, database_query, read_document, read_code, extract_field, parse_structured, symbolic_math, execute_python, execute_shell, fact_check, call_api.

---

## 3. SFT Training

**Configs**: `configs/sft/sft_qwen3_4b.yaml`, `configs/sft/sft_qwen3_8b.yaml`

| Parameter | Qwen3-4B | Qwen2.5-7B-Instruct |
|-----------|----------|---------------------|
| Framework | LlamaFactory | LlamaFactory |
| GPUs | 4x H100 80GB | 4x H100 80GB |
| Epochs | 3 | 3 |
| Effective batch | 128 (2 x 16 x 4) | 128 (1 x 32 x 4) |
| cutoff_len | 8192 (zero truncation) | 8192 (zero truncation) |
| packing | true | true |
| Learning rate | 2e-5, cosine | 2e-5, cosine |
| DeepSpeed | ZeRO-2 | ZeRO-2 |

---

## 4. RL Training

### 4.1 Algorithm: GiGPO

Two-level advantage decomposition:

| Level | GiGPO concept | SkillRouter mapping |
|-------|---------------|---------------------|
| Episode-level | Group relative advantage across N rollouts of same query | "Is this decomposition plan good?" |
| Step-level | Anchor-based grouping across trajectories at same state | "Is this (model, skill) choice good for this subtask?" |

Joint advantage: `score = episode_advantage + w x step_advantage`

### 4.2 Reward Function

```
R = (1 - alpha) x R_outcome + alpha x R_cost

R_outcome = correctness(final_answer, gold)    in {0, 1}
R_cost    = 1 - total_api_cost / MAX_COST      in [0, 1]
```

- `alpha = 0.1` (default): 90% weight on correctness, 10% on cost efficiency
- If format invalid -> R = 0

### 4.3 Environment

Each `<route>` call triggers a real API call to qwen-plus via DashScope:
- Skill-specific system prompt
- max_tokens=256, temperature=0.3
- Cost computed using the ROUTED model's pricing

### 4.4 Config

| Parameter | Value |
|-----------|-------|
| Framework | verl-agent (GiGPO) |
| GPUs | 7x H100 80GB |
| Base model | SFT checkpoint |
| train_batch_size | 112 |
| rollout.n | 8 |
| max_steps | 3 |
| total_training_steps | 100 |
| Actor LR | 1e-6 |
| KL loss | 0.001 |

---

## 5. Evaluation

### 5.1 Evaluation Pipeline

Modular framework at `eval_pipeline/` supporting any router x any benchmark.

```
eval_pipeline/
  config.py                # Model pool, cost table, skills
  run.py                   # python -m eval_pipeline.run --router X --bench Y
  routers/                 # router-r1, skillrouter-sft, direct, random, oracle
  benchmarks/              # swebench (official harness), terminalbench (Harbor Docker)
```

### 5.2 Metrics

| Metric | Description |
|--------|-------------|
| Accuracy | Resolved rate / Pass rate / EM / F1 |
| Cost | Total API cost per query (USD) |
| Pareto efficiency | Accuracy vs Cost frontier |
| Routing diversity | Entropy of (model, skill) pairs |
| Lazy rate | % queries answered without decomposition |
| Repair rate | % trajectories using verify->repair |
| DAG depth | Average subtask dependency chain length |

### 5.3 Baselines

| System | Type | Model | Paper Role |
|--------|------|-------|------------|
| Direct(Qwen2.5-7B) | No routing | Base policy model | Routing adds value over base |
| SkillRouter-SFT | Learned routing + decomp | 7B SFT checkpoint | SFT alone (no RL) |
| Direct(GPT-5.4) | No routing | Strongest single model | Upper bound |
| Router-R1 | Learned routing, no decomp | 3B pre-trained | Model-only routing |
| Oracle-Codex | Fixed -> code specialist | GPT-5.3-Codex | Is specialist always best? |
| Oracle-Strongest | Fixed -> strongest | Claude-Opus-4.6 | Cost upper bound |
| Oracle-Cheapest | Fixed -> cheapest | Claude-Haiku-4.5 | Quality lower bound |
| Random | Random model selection | Random from pool | Learned beats random? |
| WideSeek-R1 | MARL decomposition | Homogeneous agents | MARL baseline |

### 5.4 Evaluation Protocol

1. Load checkpoint
2. For each benchmark query:
   - Generate trajectory with greedy decoding (temperature=0 for router, 0.3 for sub-agents)
   - Execute routes via real API calls
   - Compute correctness + cost
3. Verify:
   - **SWE-bench**: `swebench.harness.run_evaluation` (batch Docker apply + test)
   - **Terminal-Bench**: Harbor Docker container per task (execute -> test.sh -> reward.txt)
4. Report: accuracy, cost, Pareto curve, routing diversity

---

## 6. Ablation Studies

Each ablation changes ONE variable, all else fixed.

### 6.1 SFT-only vs SFT+RL

| Experiment | Config | Purpose |
|------------|--------|---------|
| SFT-only | SFT checkpoint, no RL | What SFT alone achieves |
| SFT+RL | RL checkpoint | Incremental gain from RL |

### 6.2 Algorithm Comparison

| Experiment | Advantage | Purpose |
|------------|-----------|---------|
| GiGPO (ours) | Episode + step | Two-level credit assignment |
| GRPO | Episode only | No step grouping |
| PPO + GAE | Standard | PPO baseline |

### 6.3 Cost Weight alpha Sweep

| alpha | Behavior |
|---|---|
| 0.0 | Pure accuracy |
| 0.05 | Slight cost awareness |
| 0.1 | Default |
| 0.2 | Strong cost pressure |
| 0.5 | Heavy cost optimization |

### 6.4 Group Size N

| rollout.n | Purpose |
|-----------|---------|
| 2 | Minimum for group advantage |
| 4 | Moderate |
| 8 | Default |
| 16 | Diminishing returns? |

### 6.5 Pool Size

| Models | Skills | Purpose |
|--------|--------|---------|
| 2 (haiku, opus) | 3 (direct, search, math) | Few options |
| 5 | 6 | Medium |
| 9 | 13 | Full (current) |

---

## 7. Expected Paper Tables

### Table 1: Main Results

| System | Type | SWE-bench Resolved | TB-2.0 Pass | Avg Cost/Query |
|--------|------|:---:|:---:|---:|
| Direct(Qwen2.5-7B) | No routing | -- | -- | -- |
| Direct(GPT-5.4) | No routing | -- | -- | -- |
| Oracle-Cheapest | Fixed (haiku) | -- | -- | -- |
| Oracle-Strongest | Fixed (opus) | -- | -- | -- |
| Oracle-Codex | Fixed (codex) | -- | -- | -- |
| Random | Random | -- | -- | -- |
| Router-R1 | Learned (no decomp) | -- | -- | -- |
| **SkillRouter-SFT** | **Learned + decomp** | -- | -- | -- |
| **SkillRouter-RL** | **Learned + decomp + RL** | -- | -- | -- |

### Table 2: Routing Behavior

| Metric | Router-R1 | SkillRouter-SFT | SkillRouter-RL |
|--------|-----------|----------------|---------------|
| Avg routes/query | -- | -- | -- |
| Unique models used | -- | -- | -- |
| Skill diversity | N/A | -- | -- |
| Lazy rate | -- | -- | -- |
| DAG depth | 0 | -- | -- |

### Figure: Pareto Frontier

Accuracy (y) vs average API cost per query (x) for all baselines.
