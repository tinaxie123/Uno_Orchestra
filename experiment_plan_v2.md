# Experiment Plan v2 (SUPERSEDED)

**Status**: SUPERSEDED by `experiment_plan_v3.md`. Do not consult this file for current decisions; it is kept for historical reference only.

**Key changes in v3**:
- Main eval expanded from 3+1 to 5 benchmarks: GAIA, BrowseComp-Plus, **WideSearch**, **ToolBench**, **Terminal-Bench 2.0**
- Toolathlon moved Main → Secondary
- LiveCodeBench moved Main → Secondary

---

**Original v2 status**: Locked as the single source of truth for benchmark allocation and SFT data sourcing.

---

## 0. Hard rule (binding for the whole project)

**Any benchmark intended for the main results table MUST NOT appear in any training data — neither SFT, nor RL, nor distillation seed, nor in-context examples.**

Corollaries:
1. Eval-only benchmarks are listed in §1 and are forbidden in `config/sft_recipe.yaml` and any RL query pool.
2. 13-gram decontamination is run after every distillation batch against the union of all eval benchmark queries.
3. The few-shot examples used inside the distillation prompt MUST come from training-pool sources only, never from eval-only sources.
4. Reviewer-facing claim: *"All benchmarks reported in the main table are held out from every stage of training, including the distillation prompts."*

This rule supersedes any earlier draft that placed e.g. GAIA train, ToolBench, or LiveCodeBench inside the training mix.

---

## 1. Benchmark allocation (Train / Main Eval / Appendix)

### 1.1 Main Eval (held out from every training stage)

| Family | Benchmark | Role | Why |
|---|---|---|---|
| **Deep Research** | **GAIA** (val) | Main | Agent decomposition + tool use + verification, the central paper claim |
| | **BrowseComp-Plus** | Main | Fixed corpus retrieval — controlled environment for routing/verify/repair claim |
| | **DeepResearch Bench** | Main (one of two) | High-level deep research capability over large doc libraries |
| **Tool Use** | **Toolathlon** (Tool Decathlon) | Main | 600+ tools, long-horizon real environment — strongest differentiator vs prior work |
| | **ToolBench** (eval split only) | Secondary | Tool selection / function calling at moderate scale |
| **Coding** | **LiveCodeBench** (post-cutoff time slice) | Main | Time-isolated, the cleanest coding eval |
| | **SWE-Bench Verified** | Secondary | Heavy environment, optional final-stage report |
| | **Terminal-Bench 2.0** | Secondary | Same as above, optional |
| **Math** | **AIME 2025** | Secondary | Time-isolated, low contamination |
| **Wide Search** | **WideSearch** | Secondary | Direct comparison point against WideSeek-style baselines |
| **Multimodal** (optional) | **MMBrowseComp** | Future work | Only if a multimodal extension is added; not in v1 |

Pick **3 main benchmarks** for the main table:
- **GAIA** (deep research / agent)
- **BrowseComp-Plus** (controlled retrieval routing)
- **Toolathlon** (long-horizon tool use)

Plus **LiveCodeBench** if a coding axis is included in v1, otherwise it moves to secondary.

### 1.2 Appendix / Supporting

| Benchmark | Use |
|---|---|
| **GPQA Diamond** | General scientific reasoning, supporting evidence for collapse claim |
| **MRCR v2** | Multi-step retrieval, supporting deep-research claim |
| **LongBench v2** | Long-context reasoning, supporting evidence (not main) |
| **GSM-Hard** | Math comparison point, used in WideSeek/Puppeteer-style baselines |
| **HotpotQA dev** | In-distribution sanity check during training (NOT in main table) |

### 1.3 Explicitly excluded from main eval

- **HLE (Humanity's Last Exam)** — removed earlier, contamination risk too high.
- **MMLU** — too old, too saturated, too many models trained on it.
- **MBPP / HumanEval / MBPP+ / HumanEval+** — contamination ubiquitous; cannot bear main claims. Only acceptable as appendix sanity check.

---

## 2. Phase 1 SFT recipe (LOCKED)

**Total**: ~41,500 samples
**Distillation**: claude-opus-4-6 (~14k) + claude-sonnet-4-6 (~27.5k)
**Estimated cost**: $1,800–$2,200
**Decontamination**: 13-gram match against the union of all benchmarks listed in §1.1 and §1.2; samples with any match are dropped.

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

### 2.2 Datasets explicitly excluded from Phase 1 (with reason)

| Excluded dataset | Reason |
|---|---|
| **GAIA train** | Eval-only (rule §0); GAIA val is in the main table |
| **ToolBench** | Eval-only (rule §0); plus parsing complexity |
| **LeetCode public solutions** | License unclear |
| **SciBench** | Distribution proximity to GPQA, contamination risk |
| **NarrativeQA** | High distillation cost, uncertain return |
| **MedQA via bigbio** | Pinned dependency on bigbio package; deferred to Phase 2 with cleaner mirror |
| **ASQA / AmbigQA** | Long answers / ambiguity, schema mapping cost too high for Phase 1 |
| **ReClor** | Mirror availability uncertain |
| **MBPP+ / HumanEval+** | No train split, contamination risk if used |
| **HLE / MMLU full** | Eval-only (rule §0) |
| **AIME / AMC / FrontierMath / GSM-Hard** | Eval-only (rule §0) |
| **LiveCodeBench / SWE-Bench / Terminal-Bench** | Eval-only (rule §0) |
| **BrowseComp / BrowseComp-Plus / WideSearch / DeepResearch Bench / MMBrowseComp** | Eval-only (rule §0) |
| **Toolathlon / MRCR v2** | Eval-only (rule §0) |
| **GPQA / LongBench v2** | Eval-only (rule §0; appendix) |

### 2.3 Phase 2 (deferred — only after Phase 1 fully runs)

Triggered only if Phase 1 SFT + RL runs reveal a measurable signal gap in some domain. Candidates, in priority order:

1. NarrativeQA (summary version) — long-context signal
2. SciBench (post-decontam) — STEM depth
3. MedQA via `GBaker/MedQA-USMLE-4-options` — medical domain
4. ReClor (after availability probe) — reading reasoning
5. BAAI/TACO — code algorithmic depth
6. ASQA / AmbigQA (low weight) — ambiguity / long-answer

**ToolBench remains excluded permanently** because it is eval-only.

---

## 3. Distillation routing summary

| Tier | Model | Used for | Estimated count |
|---|---|---|---|
| **opus** | `claude-opus-4-6` | MuSiQue, StrategyQA, MATH(L3-5), TheoremQA, CodeContests, LogiQA, FOLIO, BBH, Qasper, QuALITY, LegalBench, FinQA | ~14,000 |
| **sonnet** | `claude-sonnet-4-6` | All other Phase 1 datasets | ~27,500 |

Distillation prompt enforces schema v1.1 strictly (see `data/schema_v1_1.md` §7).

Required behavioral mix in distilled output:
- ≥ 30% lazy mode (zero `<plan>`, direct `<final_answer>`)
- ≥ 30% with at least one repair round
- The remainder is one-shot success with multiple subtasks

---

## 4. Worker pool (capability tiers used at SFT and RL time)

11 workers across 7 model families. The same pool is referenced in `config/pools.yaml` and inside the system prompt of every SFT/RL sample.

| Tier | Model | Family | Role |
|---|---|---|---|
| nano | `claude-haiku-4-5-20251001` | Anthropic | cheap default |
| nano | `gemini-2.5-flash` | Google | cheap default |
| small | `gpt-5.1-low` | OpenAI | cheap reasoner |
| mid | `claude-sonnet-4-6` | Anthropic | mid-tier workhorse |
| mid | `gemini-3-pro-preview` | Google | flagship Google |
| large | `claude-opus-4-6` | Anthropic | flagship agent |
| large | `gpt-5.1-high` | OpenAI | flagship reasoner |
| large | `kimi-k2.5` | Moonshot | long-context / agent |
| large | `qwen3-max` | Alibaba | open-weight flagship |
| codex | `gpt-5.1-codex` | OpenAI | code specialist |
| codex | `qwen3-coder-plus` | Alibaba | code specialist (open) |

DeepSeek, GLM, Doubao, Grok, MiniMax, Llama: deliberately excluded.

## 5. Skill pool

```
{direct_solve, retrieval, code_exec, math_calc}
```

`direct_solve` = invoke chosen model with no external tool. This is the collapse action and is distinct from the lazy mode (no `<plan>` at all).

`web_browse` is deferred to a future version.

---

## 6. Implementation order (locked)

1. **schema v1.1** — done, `data/schema_v1_1.md` LOCKED.
2. **`scripts/schema_validator.py`** — implement all 16 rules from schema v1.1 §3 as executable checks.
3. **`config/pools.yaml`** — codify §4 and §5.
4. **`config/sft_recipe.yaml`** — codify §2.1.
5. **`scripts/distill.py`** — distillation prompt + opus/sonnet routing + validator filter + dual output (SFT messages parquet + RL prompt parquet).
6. **30-sample dry run** — 2–3 per main domain, manual review.
7. **Decontamination pass** — 13-gram against §1.1 + §1.2 union.
8. **Full Phase 1 distillation** (~41.5k).
9. **SFT warm-start training** on Qwen2.5-7B-Instruct.
10. **RL env implementation** (`hierarchical_router_parallel_env`) + GiGPO config.
11. **RL training**.
12. **Eval on main + secondary + appendix benchmarks**.

Steps 1–5 are pure local code, no compute. Step 8 is the first significant API spend. Step 11 is the first significant GPU spend.

---

## 7. Changelog

- **v1** (earlier draft): 46.5k SFT mix including GAIA train, ToolBench, LeetCode, SciBench, NarrativeQA, MedQA-bigbio.
- **v2** (this document):
  - Added §0 hard rule: main-eval benchmarks never appear in training.
  - Removed GAIA train, ToolBench, LeetCode, SciBench, NarrativeQA, MedQA, ASQA, AmbigQA, ReClor from Phase 1.
  - Reduced total to 41.5k Phase-1 clean core.
  - Locked benchmark allocation (Main / Secondary / Appendix) in §1.
  - Locked Phase 2 deferred datasets in §2.3.
  - Locked worker pool and skill pool definitions in §4 and §5.
  - Locked implementation order in §6.
