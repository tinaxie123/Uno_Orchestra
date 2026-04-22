# Baseline Result

- **SWE-bench**: `swebench.harness.run_evaluation` — batch apply patches in Docker + run test suite
- **Terminal-Bench**: Harbor Docker — start container from task image → execute solution → run `test.sh` → read `reward.txt`

## Baseline Matrix
                                           pass@1   pass@3                    pass@1   pass@3
                                            **SWE-bench**                  **Terminal-Bench**
| **Router-R1**|
| **Wideseek-R1**|
| **RouterRL(Qwen2.5-7B-Instruct)Direct** |                  
| **RouterRL(Qwen3-4B-Instruct)Direct** | 
｜ **RouterRL(Qwen2.5-7B-Instruct) Random**｜
| **RouterRL(Qwen3-4B-Instruct) Random** | 
｜**RouterRL(Qwen2.5-7B-Instruct)+ **|     
｜**RouterRL(Qwen2.5-7B-Instruct)+claude**| 
| **RouterRL(Qwen3-4B-Instruct)+** | 
| **RouterRL(Qwen3-4B-Instruct) claude** |



## Ablation Study

| **Router(Qwen2.5-7B-Instruct)Direct** |                  
| **Router(Qwen3-4B-Instruct)Direct** | 
｜ **Router(Qwen2.5-7B-Instruct) Random**｜
| **Router(Qwen3-4B-Instruct) Random** | 
｜**Router(Qwen2.5-7B-Instruct)+ **|     
｜**Router(Qwen2.5-7B-Instruct)+claude**| 
| **Router(Qwen3-4B-Instruct)+** | 
| **Router(Qwen3-4B-Instruct) claude** |
| **RouterSFT(Qwen2.5-7B-Instruct)Direct** |                  
| **RouterSFT(Qwen3-4B-Instruct)Direct** | 
｜ **RouterSFT(Qwen2.5-7B-Instruct) Random**｜
| **RouterSFT(Qwen3-4B-Instruct) Random** | 
｜**RouterSFT(Qwen2.5-7B-Instruct)+ **|     
｜**RouterSFT(Qwen2.5-7B-Instruct)+claude**| 
| **RouterSFT(Qwen3-4B-Instruct)+** | 
| **RouterSFT(Qwen3-4B-Instruct) claude** |


### Cost Table (official pricing, no Qwen in sub-agent pool)

| Model | $/1M input | $/1M output |
|-------|---:|---:|
| gemini-2.5-flash-lite | $0.10 | $0.40 |
| gemini-2.5-flash | $0.30 | $2.50 |
| kimi-k2.5 | $0.35 | $2.50 |
| gemini-3-flash-preview | $0.50 | $3.00 |
| claude-haiku-4-5-20251001 | $1.00 | $5.00 |
| gpt-5.3-codex | $1.75 | $14.00 |
| gpt-5.4 | $2.50 | $15.00 |
| claude-sonnet-4-6 | $3.00 | $15.00 |
| claude-opus-4-6 | $5.00 | $25.00 |

---

## 4. Infrastructure

### 4.1 Compute

| Resource | Allocation |
|----------|-----------|
| GPU 2 (H100 80GB) | vLLM: SkillRouter-SFT (7B), port 8000 |
| GPU 3 (H100 80GB) | vLLM: Qwen2.5-7B-Instruct (base), port 8001 |
| Docker | 24 containers active (Terminal-Bench verification) |
| API (external API) | 12 eval processes, ~16 concurrent workers each |

### 4.2 Model Checkpoints

| Model | Path | Size |
|-------|------|------|
| Router-R1 | `/data/xieht/models/Router-R1-Qwen2.5-3B-Instruct` | 3B |
| SkillRouter-SFT | `/home/xieht/data/sft/checkpoints/router_qwen25_7b_full_sft` | 7B |
| Qwen2.5-7B-Instruct | `/data/xieht/models/Qwen/Qwen2.5-7B-Instruct-real` | 7B |



## Case Study

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

Sub-agent calls use real API models via external API (not DashScope proxy).
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
| router+claude | Claude-Opus worker pool | — | — | $0.063 |
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
    │● router+claude (expensive)
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
