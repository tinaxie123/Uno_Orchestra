# Evaluation Status Report

**Date**: 2026-04-13
**Target**: NeurIPS 2026
**Project**: Learned Selective Delegation for Multi-Agent Systems (SkillRouter)

---

## 1. Evaluation Framework

Unified pipeline at `eval_pipeline/` supporting any router × any benchmark.

```
eval_pipeline/
├── config.py               # Single source: 9-model pool, cost table, 13 skills
├── run.py                  # Entry: python -m eval_pipeline.run --router X --bench Y
├── routers/
│   ├── base.py             # BaseRouter interface → RouteResult
│   ├── router_r1.py        # Router-R1 (3B, <think>→<search>→<answer>)
│   ├── skillrouter_sft.py  # SkillRouter SFT (7B, <plan>→<route>→<obs>→<final_answer>)
│   ├── direct.py           # Single model, no routing
│   ├── random_router.py    # Random model selection
│   └── oracle.py           # Fixed model (cheapest/strongest/codex)
└── benchmarks/
    ├── swebench.py         # SWE-bench Verified (swebench harness, official Docker eval)
    └── terminalbench.py    # Terminal-Bench 2.0 (Harbor Docker, test.sh verification)
```

**Verification methods** (both official):
- **SWE-bench**: `swebench.harness.run_evaluation` — batch apply patches in Docker + run test suite
- **Terminal-Bench**: Harbor Docker — start container from task image → execute solution → run `test.sh` → read `reward.txt`

---

## 2. Baseline Matrix

| # | Baseline | Type | Model | Pool | Paper Role |
|---|----------|------|-------|------|------------|
| 1 | **Direct(Qwen2.5-7B)** | No routing | Qwen2.5-7B-Instruct (base) | — | Shows routing adds value over base model |
| 2 | **SkillRouter-SFT** | Learned routing + decomposition | Qwen2.5-7B SFT checkpoint | 9 models × 13 skills | Ablation: SFT alone (no RL) |
| 3 | **Direct(GPT-5.4)** | No routing | GPT-5.4 (strongest) | — | Upper bound: best single model |
| 4 | **Router-R1** | Learned routing, no decomposition | Router-R1-Qwen2.5-3B | 9 models | External baseline: model-only routing |
| 5 | **Oracle-Codex** | Fixed routing → code specialist | GPT-5.3-Codex always | — | Is code specialist always best for code tasks? |
| 6 | **Oracle-Strongest** | Fixed routing → strongest | Claude-Opus-4.6 always | — | Cost upper bound: always pick most expensive |
| 7 | **Oracle-Cheapest** | Fixed routing → cheapest | Claude-Haiku-4.5 always | — | Quality lower bound: always pick cheapest |
| 8 | **Random** | Random routing | Random from pool | 9 models | Does learned routing beat random? |

**Missing** (TODO):
- **SkillRouter-RL** (Phase E checkpoint) — final method, not yet trained
- **WideSeek-R1** — external MARL baseline

---

## 3. Current Progress

### 3.1 Generation Phase (Router → predictions)

| Baseline | SWE-bench (500) | Terminal-Bench (89) | Status |
|----------|:---:|:---:|--------|
| Router-R1 | 493/500 | 89/89 ✅ | Generation nearly complete |
| SkillRouter-SFT | 38/500 | 18/89 | **Running on GPU 2** |
| Direct(Qwen2.5-7B) | 123/500 | 79/89 | **Running on GPU 3** |
| Direct(GPT-5.4) | 500/500 ✅ | 89/89 ✅ | Complete |
| Oracle-Codex | 500/500 ✅ | 89/89 ✅ | Complete |
| Oracle-Strongest | 500/500 ✅ | 89/89 ✅ | Complete |
| Oracle-Cheapest | 500/500 ✅ | 89/89 ✅ | Complete |
| Random | 286/500 | 87/89 | Running (API only) |

### 3.2 Verification Phase (Docker execution → real pass rate)

**SWE-bench** (official swebench harness):

| Baseline | Verified | Resolved | Resolved Rate | Cost/Instance |
|----------|:---:|:---:|:---:|---:|
| Oracle-Codex | 500 | TBD | TBD | $0.0068 |
| Oracle-Strongest | 500 | TBD | TBD | $0.0632 |
| Oracle-Cheapest | 500 | TBD | TBD | ~$0.0000 |
| Direct(GPT-5.4) | 500 | TBD | TBD | $0.0190 |
| Router-R1 | pending | — | — | ~$0.015 |
| Others | pending | — | — | — |

> Note: SWE-bench harness has been invoked for 4 baselines. Results parsing in progress.

**Terminal-Bench** (Docker sandbox execution):

| Baseline | Verified / Total | Passed | Pass Rate |
|----------|:---:|:---:|:---:|
| Router-R1 | 14/89 | 0 | 0.0% (in progress) |
| Oracle-Strongest | 13/89 | 0 | 0.0% (in progress) |
| Random | 12/89 | 0 | 0.0% (in progress) |
| Oracle-Cheapest | 10/89 | 0 | 0.0% (in progress) |
| Direct(GPT-5.4) | 9/89 | 0 | 0.0% (in progress) |
| Oracle-Codex | 6/89 | 0 | 0.0% (in progress) |

> Note: Terminal-Bench verification is running in pipeline mode (generate + Docker verify concurrently, 24 containers active). 0% pass rate may be expected for single-shot non-agentic approaches — Terminal-Bench tasks require multi-step interactive debugging.

---

## 4. Infrastructure

### 4.1 Compute

| Resource | Allocation |
|----------|-----------|
| GPU 2 (H100 80GB) | vLLM: SkillRouter-SFT (7B), port 8000 |
| GPU 3 (H100 80GB) | vLLM: Qwen2.5-7B-Instruct (base), port 8001 |
| Docker | 24 containers active (Terminal-Bench verification) |
| API (xiaojingai) | 12 eval processes, ~16 concurrent workers each |

### 4.2 Model Checkpoints

| Model | Path | Size |
|-------|------|------|
| Router-R1 | `/data/xieht/models/Router-R1-Qwen2.5-3B-Instruct` | 3B |
| SkillRouter-SFT | `/home/xieht/data/sft/checkpoints/router_qwen25_7b_full_sft` | 7B |
| Qwen2.5-7B-Instruct | `/data/xieht/models/Qwen/Qwen2.5-7B-Instruct-real` | 7B |

### 4.3 Cost Table (aligned with SkillRouter envs.py)

| Model | $/1M output tokens | Tier |
|-------|---:|------|
| claude-haiku-4-5-20251001 | $1.25 | nano |
| gemini-2.5-flash | $1.50 | nano |
| kimi-k2.5 | $2.00 | large |
| qwen3.6-plus | $8.00 | large |
| gemini-3.1-pro-preview | $10.00 | large |
| claude-sonnet-4-6 | $15.00 | mid |
| gpt-5.3-codex | $20.00 | code |
| gpt-5.4 | $60.00 | large |
| claude-opus-4-6 | $75.00 | large |

---

## 5. Key Design Decisions

### 5.1 Router-R1 Engineering Fixes (vs original paper)

| Issue | Fix |
|-------|-----|
| `<search>LLM-Name:...` literal placeholder | Fuzzy model name resolution with fallback |
| No cost tracking | Per-token cost computation using pool pricing |
| `temperature=1.0` (noisy) | `temperature=0.0` (greedy, matching eval protocol) |
| `max_tokens=512` (too small for patches) | `4096` for eval |
| `gpt-4o-mini` not in pool | Replaced with `claude-haiku-4-5-20251001` |
| Prompt biases routing to codex | Removed "you SHOULD delegate to..." |

### 5.2 SkillRouter-SFT Adapter

The SFT model uses schema v1.1 (multi-turn):
```
[ASSISTANT] <plan round="1"> <subtask>... <route model="X" skill="Y">...
[TOOL]      <obs subtask="1">real API response</obs>
[ASSISTANT] <verify round="1" status="pass"> <final_answer>...
```

Sub-agent calls use real API models via xiaojingai (not DashScope proxy).
This matches the eval protocol in experiment_plan_v3.md §5.3.

### 5.3 Fair Comparison Principles

1. **Same model pool**: All routers share the 9-model pool from `configs/pools.yaml`
2. **Same cost computation**: USD per 1M tokens from `envs.py`
3. **Same verification**: Official harness (SWE-bench) / Harbor Docker (Terminal-Bench)
4. **Same eval protocol**: Greedy decoding (temp=0) for router, temp=0.3 for sub-agents

---

## 6. Expected Paper Table

### Table 1: Main Results

| System | Type | SWE-bench Resolved↑ | TB-2.0 Pass↑ | Avg Cost↓ |
|--------|------|:---:|:---:|---:|
| Direct(Qwen2.5-7B) | No routing | — | — | — |
| Direct(GPT-5.4) | No routing | — | — | $0.019 |
| Oracle-Cheapest | Fixed (haiku) | — | — | ~$0.00 |
| Oracle-Strongest | Fixed (opus) | — | — | $0.063 |
| Random | Random | — | — | — |
| Router-R1 | Learned (no decomp) | — | — | ~$0.015 |
| **SkillRouter-SFT** | **Learned + decomp** | — | — | — |
| **SkillRouter-RL** | **Learned + decomp + RL** | — | — | — |

### Table 2: Routing Diversity (Router-R1 vs SkillRouter)

| Metric | Router-R1 | SkillRouter-SFT |
|--------|-----------|----------------|
| Avg routes/query | ~1.0 | TBD |
| Model entropy | TBD | TBD |
| Skill diversity | N/A (no skills) | TBD |
| Lazy rate | TBD | TBD |
| Decomposition depth | 0 (no DAG) | TBD |

### Figure: Pareto Frontier (Accuracy vs Cost)

```
Accuracy ↑
    │     ● SkillRouter-RL (target)
    │   ● SkillRouter-SFT
    │  ● Router-R1
    │ ● GPT-5.4 direct
    │● Oracle-Strongest (expensive)
    │
    │         ● Random
    │              ● Oracle-Cheapest (cheap but weak)
    └────────────────────────── Cost →
```

---

## 7. TODO

### Immediate (Running)
- [ ] Complete all generation (Random SWE-bench, SFT, Base Qwen)
- [ ] Complete Terminal-Bench Docker verification for all baselines
- [ ] Parse SWE-bench harness reports for resolved rates

### Next Steps
- [ ] SkillRouter-RL checkpoint (Phase E) → add to eval
- [ ] WideSeek-R1 baseline
- [ ] Secondary benchmarks: GAIA, MMLU, GSM-Hard, GPQA, HumanEval, MBPP
- [ ] Statistical significance (multiple seeds / confidence intervals)
- [ ] Pareto curve generation
- [ ] Ablation: α sweep, group size N, pool size

### Investigation Needed
- [ ] SWE-bench 0% resolved — expected for single-shot? Or harness issue?
- [ ] Terminal-Bench 0% pass — expected for non-agentic? Need to check Docker logs
- [ ] Router-R1 routing diversity — is it still degenerate after prompt fix?

---

## 8. Commands Reference

```bash
cd /data/xieht/multiagentRL

# Run any router × benchmark
python -m eval_pipeline.run --router ROUTER --bench BENCH --api_key KEY

# Available routers:
#   router-r1, skillrouter-sft, direct, random,
#   oracle-cheapest, oracle-strongest, oracle-codex

# Available benchmarks:
#   swebench, terminalbench

# Options:
#   --local_base URL        vLLM endpoint for local models
#   --local_model NAME      vLLM model name
#   --direct_model NAME     API model for direct router
#   --gen_workers N         Parallel generation workers
#   --verify_workers N      Parallel Docker verification workers
#   --skip_gen              Skip generation, use cached predictions
#   --skip_verify           Skip verification
#   --max_tasks N           Limit number of tasks

# Check progress:
tail -3 /data/xieht/eval_results/ROUTER_BENCH_run.log
wc -l /data/xieht/eval_results/ROUTER_BENCH/predictions.jsonl
wc -l /data/xieht/eval_results/ROUTER_BENCH/verification.jsonl
```
