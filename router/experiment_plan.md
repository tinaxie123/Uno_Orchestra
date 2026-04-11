# Experiment Plan v3 (LOCKED)

**Status**: Locked. Supersedes `experiment_plan_v2.md`. Single source of truth for benchmark allocation, training data sourcing, and contamination policy.

**Relation to other locked documents**:
- `data/trajectory_schema.md` — trajectory schema (LOCKED)
- This file — benchmark allocation + Phase 1 SFT recipe (LOCKED)
- `config/pools.yaml` — worker + skill pools (to be written, must conform to §4–§5 here)
- `config/sft_recipe.yaml` — Phase 1 recipe (to be written, must conform to §2.1 here)

---

## 0. Hard Rule (binding for the whole project)

**Any benchmark family used in evaluation MUST NOT appear in any training stage — neither SFT, nor RL, nor distillation seed, nor as in-context examples in distillation prompts.**

Corollaries:
1. Eval-only benchmark families are listed in §1 and forbidden in `config/sft_recipe.yaml` and any RL query pool.
2. 13-gram decontamination is run after every distillation batch against the union of all eval-only benchmark queries.
3. Distillation prompt few-shot examples MUST come exclusively from training-pool sources.
4. Reviewer-facing claim: *"All benchmarks reported in the main and secondary tables are held out from every stage of training, including the distillation prompts."*

This rule supersedes any earlier draft that placed e.g. GAIA train, ToolBench, or LiveCodeBench inside training data.

---

## 1. Benchmark Split

### 1.1 Training Data

We train the model only on non-benchmark datasets and distilled trajectories generated under the locked `schema_v1.1`. The goal is to teach the model four behaviors:

1. when **not** to decompose,
2. when to decompose,
3. how to route a subtask to `direct_solve` / `retrieval` / `code_exec` / tool,
4. when to trigger repair.

The Phase-1 clean training mix includes:

- **Multi-hop QA**: HotpotQA (fullwiki), 2WikiMultihopQA, MuSiQue, StrategyQA
- **Single-hop / lazy supervision**: NQ Open, TriviaQA (rc.nocontext), WebQuestions
- **Math**: GSM8K, MATH, TheoremQA, AQuA-RAT
- **Coding**: APPS, CodeContests
- **STEM**: SciQ, ARC-Challenge, OpenBookQA, MMLU auxiliary_train (STEM slice)
- **Commonsense / social reasoning**: CommonsenseQA, PIQA, Social IQA, Winogrande
- **Formal reasoning**: LogiQA 2.0, FOLIO, BBH logic subset
- **Long-context reading**: Qasper, QuALITY
- **Domain knowledge**: LegalBench, FinQA
- **Tool-use supervision**: API-Bank, ToolACE

All benchmark families used in the main and secondary evaluations are excluded from training to avoid contamination.

### 1.2 Main Evaluation

Our main evaluation focuses on held-out agentic benchmarks that directly test decomposition, routing, retrieval, verification, and repair.

| Benchmark | Role |
|---|---|
| **GAIA** | Primary benchmark for long-horizon multi-tool reasoning |
| **BrowseComp-Plus** | Controlled deep-research benchmark with fixed retrieval conditions |
| **WideSearch** | Primary benchmark for parallel decomposition and broad information seeking |
| **ToolBench** | Held-out benchmark for tool routing and function selection |
| **Terminal-Bench 2.0** | Coding-agent benchmark for execution-heavy decision making |

These benchmarks are reserved exclusively for evaluation and are never used in SFT or RL training.

### 1.3 Secondary Evaluation

Secondary benchmarks test transfer beyond the primary settings:

- **DeepResearch Bench** — large-doc-library deep research
- **MMBrowseComp** — multimodal browsing (future-work axis)
- **Tool Decathlon / Toolathlon** — long-horizon real-environment tool use
- **MRCR v2** — multi-step retrieval reasoning
- **LiveCodeBench** — time-isolated coding evaluation
- **SWE-bench** — repository-level coding

These help measure generalization to multimodal browsing, long-horizon tool use, and repo-level coding tasks. They do not define the central claim of the paper.

### 1.4 Appendix / Supporting Evaluation

Used for sanity checks and broader capability profiling:

- AIME
- AMC
- GSM-Hard
- FrontierMath (if accessible)
- GPQA
- MMLU
- LongBench v2
- MBPP
- HumanEval

These are not the most faithful tests of our main claim (optional decomposition with capability routing and iterative repair), so they sit in supporting tables only.

### 1.5 Contamination Policy

We adopt a strict split between training data and benchmark families used for evaluation. The following datasets are evaluation-only and excluded from all SFT and RL training:

- GAIA
- BrowseComp / BrowseComp-Plus
- WideSearch
- DeepResearch Bench
- MMBrowseComp
- ToolBench
- Tool Decathlon / Toolathlon
- MRCR v2
- SWE-bench
- Terminal-Bench 2.0
- LiveCodeBench

This policy ensures that the reported gains reflect held-out generalization rather than benchmark familiarity.

### 1.6 Rationale

This benchmark split matches the paper's core claim. Training data teaches the model the structural behaviors needed for **single-shot decomposition with iterative repair**, while held-out benchmarks test whether those behaviors transfer to realistic deep-research, tool-use, and coding-agent settings.

---

## 2. Phase 1 SFT Recipe (LOCKED)

**Total**: ~41,500 samples
**Distillation**: `claude-opus-4-6` (~14k) + `claude-sonnet-4-6` (~27.5k)
**Estimated cost**: $1,800–$2,200
**Decontamination**: 13-gram match against the union of §1.5 + all benchmarks listed in §1.2/§1.3/§1.4. Matching samples are dropped.

### 2.1 Composition

| Domain | Dataset | HF path | n | Distill |
|---|---|---|---|---|
| **Multi-hop QA (11,000)** | HotpotQA (fullwiki) | `hotpot_qa` | 4000 | sonnet |
| | 2WikiMultihopQA | `voidful/2WikiMultihopQA` | 3000 | sonnet |
| | MuSiQue (answerable) | `dgslibisey/MuSiQue` | 2500 | opus |
| | StrategyQA | `voidful/StrategyQA` | 1500 | opus |
| **Single-hop / Lazy (6,000)** | NaturalQuestions | `nq_open` | 3000 | sonnet |
| | TriviaQA (rc.nocontext) | `mandarjoshi/trivia_qa` | 2000 | sonnet |
| | WebQuestions | `web_questions` | 1000 | sonnet |
| **Math (6,000)** | GSM8K | `openai/gsm8k` | 2000 | sonnet |
| | MATH (level 3-5) | `lighteval/MATH` | 2500 | opus |
| | TheoremQA | `wenhu/TheoremQA` | 800 | opus |
| | AQuA-RAT | `deepmind/aqua_rat` | 700 | sonnet |
| **Code (5,000)** | APPS (intro+interview) | `codeparrot/apps` | 2500 | sonnet |
| | CodeContests | `deepmind/code_contests` | 2500 | opus |
| **STEM (4,500)** | SciQ | `allenai/sciq` | 1000 | sonnet |
| | ARC-Challenge | `allenai/ai2_arc` | 1000 | sonnet |
| | OpenBookQA | `allenai/openbookqa` | 800 | sonnet |
| | MMLU auxiliary_train (STEM slice) | `cais/mmlu` (`auxiliary_train`) | 1700 | sonnet |
| **Commonsense / Social (4,000)** | CommonsenseQA | `tau/commonsense_qa` | 1500 | sonnet |
| | PIQA | `ybisk/piqa` | 1000 | sonnet |
| | Social IQA | `allenai/social_i_qa` | 800 | sonnet |
| | Winogrande | `allenai/winogrande` | 700 | sonnet |
| **Formal logic (1,800)** | LogiQA 2.0 | `lighteval/logiqa2` | 800 | opus |
| | FOLIO | `yale-nlp/FOLIO` | 600 | opus |
| | BBH (logic subset) | `lukaemon/bbh` | 400 | opus |
| **Long context (900)** | Qasper | `allenai/qasper` | 500 | opus |
| | QuALITY | `emozilla/quality` | 400 | opus |
| **Domain knowledge (900)** | LegalBench (5–8 subtasks balanced) | `nguha/legalbench` | 500 | opus |
| | FinQA | `dreamerdeo/finqa` | 400 | opus |
| **Tool / Agent (1,400)** | API-Bank | `liyucheng/api-bank` | 600 | sonnet |
| | ToolACE | `Team-ACE/ToolACE` | 800 | sonnet |
| **TOTAL** | | | **41,500** | |

### 2.2 Datasets explicitly excluded from Phase 1

| Excluded dataset | Reason |
|---|---|
| GAIA train | Eval-only (rule §0) |
| ToolBench train | Eval-only (rule §0) |
| LeetCode public solutions | License unclear |
| SciBench | Distribution proximity to GPQA |
| NarrativeQA | High distillation cost, uncertain return |
| MedQA via bigbio | Dependency issues; deferred to Phase 2 with cleaner mirror |
| ASQA / AmbigQA | Long answers / ambiguity, schema mapping cost too high |
| ReClor | Mirror availability uncertain |
| MBPP+ / HumanEval+ | No train split, contamination risk |
| HLE / MMLU full | Eval-only (rule §0; appendix only) |
| AIME / AMC / FrontierMath / GSM-Hard | Eval-only (rule §0) |
| LiveCodeBench / SWE-Bench / Terminal-Bench 2.0 | Eval-only (rule §0) |
| BrowseComp / BrowseComp-Plus / WideSearch / DeepResearch Bench / MMBrowseComp | Eval-only (rule §0) |
| Toolathlon / MRCR v2 | Eval-only (rule §0) |
| GPQA / LongBench v2 | Eval-only (rule §0; appendix) |

### 2.3 Phase 2 (deferred — only after Phase 1 fully runs)

Triggered only if Phase 1 SFT + RL runs reveal a measurable signal gap in some domain. Candidates, in priority order:

1. NarrativeQA (summary version) — long-context signal
2. SciBench (post-decontam) — STEM depth
3. MedQA via `GBaker/MedQA-USMLE-4-options` — medical domain
4. ReClor (after availability probe) — reading reasoning
5. BAAI/TACO — code algorithmic depth
6. ASQA / AmbigQA (low weight) — ambiguity / long-answer

**ToolBench remains excluded permanently** — it is in the main evaluation table.

---

## 3. Distillation Routing Summary

| Tier | Model | Used for | Estimated count |
|---|---|---|---|
| opus | `claude-opus-4-6` | MuSiQue, StrategyQA, MATH (L3-5), TheoremQA, CodeContests, LogiQA, FOLIO, BBH, Qasper, QuALITY, LegalBench, FinQA | ~14,000 |
| sonnet | `claude-sonnet-4-6` | All other Phase 1 datasets | ~27,500 |

Distillation prompt enforces schema strictly (see `data/trajectory_schema.md` §7).

Required behavioral mix in distilled output:
- ≥ 30% lazy mode (no `<plan>`, direct `<final_answer>`)
- ≥ 30% with at least one repair round
- Remainder: one-shot success with multiple subtasks

---

## 4. Worker Pool

7 executor models: 6 family flagships + 1 cheap floor. Same pool is referenced in `config/pools.yaml` and inside the system prompt of every SFT/RL sample. The training backbone (`Qwen2.5-7B-Instruct`) is the **policy model** and is forbidden as a `<route>` target.

| Tier | Model | Family | Role | cost_rank |
|---|---|---|---|---|
| nano | `claude-haiku-4-5-20251001` | Anthropic | Cheap floor (cost-aware collapse anchor) | 1 |
| large | `claude-opus-4-6` | Anthropic | Frontier agent (SOTA on GAIA / SWE-Bench) | 5 |
| large | `gpt-5.1-high` | OpenAI | Frontier reasoning | 5 |
| code-specialist | `gpt-5.1-codex` | OpenAI | Code specialist | 4 |
| large | `gemini-3-pro-preview` | Google | Frontier general + long context | 3 |
| large | `kimi-k2.5` | Moonshot | Long-context agent | 2 |
| large | `grok-4.1` | xAI | Reasoning alternative channel | 3 |

DeepSeek, GLM, Doubao, MiniMax, Hunyuan, old Llama: deliberately excluded — do not meet the pool quality bar for our target benchmarks.

Per-model `allowed_skills` whitelists are defined in `config/pools.yaml` and enforced by `scripts/validate_schema.py` in strict mode (default). Setting `enforcement: free` disables the whitelist for ablation.

---

## 5. Skill Pool

Phase 1: 6 skills.

```
{direct_solve, retrieval, read_extract, code_exec, table_ops, browser_use}
```

| Skill | Executor | Notes |
|---|---|---|
| `direct_solve` | none | The collapse action: model answers from parametric knowledge alone. Distinct from lazy mode (no `<plan>` at all). |
| `retrieval` | search (BM25 / Tavily) | Returns ranked passages |
| `read_extract` | document_reader | Extract structured fragment from a known document |
| `code_exec` | python sandbox | Execute generated Python, return stdout/value |
| `table_ops` | tabular (pandas / sql-lite) | Joins, filters, aggregations on a given table |
| `browser_use` | playwright | Click / scroll / fill on dynamic web pages |

Phase 2 candidates (not in v1): `entity_disambiguate`, `citation_grounding`, `compare_rank`, `tool_api_call`, `image_read`, `ocr_extract`. Tracked as comments in `config/pools.yaml`.

`subagent` is **explicitly NOT** in the skill pool. Following the collapse framing of the paper, the Router never selects "spawn a stateful subagent"; that is the WideSeek-style action our work argues against.

---

## 6. Implementation Order (LOCKED)

1. **schema** — done, `data/trajectory_schema.md` LOCKED.
2. **`scripts/validate_schema.py`** — implement all 16 rules from schema §3 as executable checks.
3. **`config/pools.yaml`** — codify §4 and §5.
4. **`config/sft_recipe.yaml`** — codify §2.1.
5. **`scripts/generate_trajectories.py`** — distillation prompt + opus/sonnet routing + validator filter + dual output (SFT messages parquet + RL prompt parquet).
6. **30-sample dry run** — 2–3 per main domain, manual review.
7. **Decontamination pass** — 13-gram against the union of §1.2 + §1.3 + §1.4.
8. **Full Phase 1 distillation** (~41.5k).
9. **SFT warm-start training** on Qwen2.5-7B-Instruct.
10. **RL env implementation** (`hierarchical_router_parallel_env`) + GiGPO config.
11. **RL training**.
12. **Eval on main + secondary + appendix benchmarks**.

Steps 1–5 are pure local code, no compute. Step 8 is the first significant API spend. Step 11 is the first significant GPU spend.

---

## 7. Changelog

- **v1**: 46.5k SFT mix including GAIA train, ToolBench, LeetCode, SciBench, NarrativeQA, MedQA-bigbio.
- **v2**: Added §0 hard rule. Removed all eval-only datasets from Phase 1. Reduced to 41.5k. Locked benchmark allocation (Main: GAIA / BrowseComp-Plus / Toolathlon, Secondary: ToolBench / WideSearch / SWE-Bench / Terminal-Bench 2.0 / AIME / DeepResearch Bench).
- **v3** (this document): Reallocated benchmarks. **Main expanded to 5**: GAIA, BrowseComp-Plus, WideSearch, ToolBench, Terminal-Bench 2.0. **Toolathlon and LiveCodeBench moved to Secondary**. Rationale: prioritize benchmarks with stable runnable environments and high community recognition; defer Toolathlon (heavy environment) and LiveCodeBench (time-slicing care needed) to secondary.
