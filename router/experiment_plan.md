# Experiment Plan

## Contamination Policy

**Any benchmark family used in evaluation MUST NOT appear in any training stage** — neither SFT, nor RL, nor distillation prompts.

All benchmarks reported in main and secondary tables are held out from every stage of training. 13-gram decontamination is run after every distillation batch.

---

## 1. Benchmark Split

### 1.1 Training Data

We train the model on non-benchmark datasets and distilled trajectories. The goal is to teach four behaviors: (1) when not to decompose, (2) when to decompose, (3) how to route subtasks to (model, skill) pairs, (4) when to trigger repair.

Training mix: 31 datasets across 10 domains (see `config/sft_recipe.yaml`):

- **Multi-hop QA**: HotpotQA, 2WikiMultihopQA, MuSiQue, StrategyQA
- **Single-hop / Lazy**: NQ Open, TriviaQA, WebQuestions
- **Math**: GSM8K, MATH (algebra/intermediate/number_theory), TheoremQA, AQuA-RAT
- **Code**: Codeforces-CoTs, CodeContests
- **STEM**: SciQ, ARC-Challenge, OpenBookQA, MMLU auxiliary_train (STEM)
- **Commonsense / Social**: CommonsenseQA, PIQA, Social IQA, Winogrande
- **Formal Logic**: LogiQA 2.0, FOLIO, BBH (logical_deduction, formal_fallacies)
- **Long Context**: QuALITY
- **Domain Knowledge**: LegalBench, FinQA
- **Tool Agent**: ToolACE

### 1.2 Main Evaluation (held out)

| Benchmark | Role |
|---|---|
| GAIA | Long-horizon multi-tool reasoning |
| BrowseComp-Plus | Deep-research with fixed retrieval |
| WideSearch | Parallel decomposition, broad information seeking |
| ToolBench | Tool routing and function selection |
| Terminal-Bench 2.0 | Execution-heavy coding agent |

### 1.3 Secondary Evaluation (held out)

DeepResearch Bench, MMBrowseComp, Toolathlon, MRCR v2, LiveCodeBench, SWE-bench.

### 1.4 Supporting Evaluation (held out)

AIME, AMC, GSM-Hard, GPQA, MMLU, LongBench v2, MBPP, HumanEval.

---

## 2. SFT Recipe

- **Total**: 58,457 samples (target 41,500, exceeded due to multi-source generation)
- **Teachers**: claude-sonnet-4-6 (69%), claude-opus-4-6 (18%), qwen-max (13%)
- **Cost**: $82.95

Behavioral mix in output:
- Lazy (direct answer): 15.6%
- One-shot decomposition: 49.5%
- Observation-driven continuation: 30.4%
- Decomposition repair: 4.4%

---

## 3. Worker Pool

9 executor models across 5 families (see `config/pools.yaml`):

| Tier | Model | Family | Cost |
|------|-------|--------|------|
| nano | claude-haiku-4-5-20251001 | Anthropic | 1 |
| mid | claude-sonnet-4-6 | Anthropic | 3 |
| large | claude-opus-4-6 | Anthropic | 5 |
| large | gpt-5.4 | OpenAI | 5 |
| code | gpt-5.3-codex | OpenAI | 4 |
| large | gemini-3.1-pro-preview | Google | 3 |
| nano | gemini-2.5-flash | Google | 1 |
| large | kimi-k2.5 | Moonshot | 2 |
| large | qwen3.6-plus | Alibaba | 4 |

Policy model (Qwen2.5-7B-Instruct) is the training backbone and forbidden as a route target.

## 4. Skill Pool

13 skills:

| Skill | Description |
|-------|-------------|
| direct_answer | Answer from parametric knowledge, no external action |
| reason | Extended multi-step inference |
| web_search | Broad information retrieval from web/RAG |
| database_query | Query structured database via SQL |
| read_document | Comprehend a known document |
| read_code | Understand codebase or function |
| extract_field | Pull specific datum from a source |
| parse_structured | Navigate complex structured data (JSON, XML, CSV) |
| symbolic_math | Symbolic + numeric calculation |
| execute_python | Write and execute Python in sandbox |
| execute_shell | Execute bash commands |
| fact_check | Verify claim against evidence |
| call_api | Invoke external API via structured args |

---

## 5. Implementation Phases

| Phase | Task | Status |
|-------|------|--------|
| A | Schema design (`data/trajectory_schema.md`) | DONE |
| B | Pilot distillation (30 → 400 samples) | DONE |
| C | Full distillation (58,457 samples) | DONE |
| D | SFT training (Qwen2.5-7B, 8x H100) | NEXT |
| E | RL fine-tuning (PPO, reward = correctness - lambda * cost) | PLANNED |
| F | Evaluation on held-out benchmarks | PLANNED |
