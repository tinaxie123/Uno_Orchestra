# 🎉 Uno-Orchestra

> 💗 We propose **Uno-Orchestra** — a 7b router model that, given a task, decomposes it into subtasks and dispatches each to a `(worker model, skill)` pair. Uno-Orchestra is trained in two stages：📚 **SFT** on distilled trajectories 💰 **cost-aware GRPO** 
>
> 🏗️ built on top of [**verl**](https://github.com/volcengine/verl), whose training stack made the RL side tractable. 

## 🎆Configure Your Own Uno-router

### 🍬Data Source Pool Construction

**🍰 Data Source pool construction.**

A router must learn **when** and **how** to decompose a task. According to the capability taxonomy of general AI assistants (Mialon et al., 2024), we organize our data along four dimensions reflecting different decomposition patterns:

🍓**reasoning**, where a problem must be broken into a chain of inferential or computational steps;

🥭**knowledge retrieval**, where the router issues parallel or sequential queries to gather information from multiple sources;

🍊**tool use**, where sub-tasks involve heterogeneous operations such as code execution or API calls;

🍑**multi-step planning**, where intermediate results shape subsequent actions.


We pick a minimal set of **anchor sources** such that each of the four capability dimensions is covered by at least one dataset and every anchor contributes a decomposition pattern the others do not — GSM8K, NuminaMath-CoT, DROP, HotpotQA, MuSiQue, TACO, ToolACE — and then supplement with a broader tail of open-domain QA, commonsense, and academic-knowledge datasets to thicken coverage at each level. Two inclusion criteria gate every source: **(i) the task must exercise the router's decision-making capability, spanning both single-step tasks where the router learns to dispatch directly to an appropriate model and multi-step tasks where it must decompose the problem into dependent sub-tasks; (ii) gold answers must be automatically verifiable to enable scalable filtering.**

| Capability axis | What the router must learn | Datasets |
|---|---|---|
| **Atomic reasoning** | Forward the task to a single model | GSM8K |
| **Compositional reasoning** | Multi-step symbolic manipulation requiring chain-of-thought delegation | NuminaMath-CoT |
| **Knowledge retrieval** | Decompose into independent evidence-gathering subtasks | DROP, HotpotQA |
| **Knowledge composition** | Deep sequential decomposition with inter-subtask dependencies | MuSiQue |
| **Tool orchestration** | Select correct tool–model pairs and chain API calls | TACO, ToolACE |

### 🍭Data Selection Pipeline

The pipeline turns the raw ~10 k task pool into the final 61,201-trajectory SFT corpus in **five phases**. Re-running it after every training round yields a curriculum of increasing difficulty, since the router's capability boundary shifts as it improves.

**Phase 1 — Stratified coverage sampling.** Fixed per-source quotas so each of the five capability axes above is exercised by at least one dataset and no axis dominates. Output: ≈ 10 k raw tasks.

**Phase 2 — Bootstrapped curriculum filtering.** Three stages over every task in the pool:

1. *Router probe* — run the current router checkpoint with real sub-model execution and score pass@3 against gold. Tasks the router already solves are discarded (no learning signal).
2. *Teacher trajectory* — for each remaining task, run a strong teacher orchestrator. Correct teacher trajectories enter the SFT set; tasks where the teacher also fails enter the RL pool, where the router must discover a decomposition through its own exploration.
3. *Noise removal* — drop trajectories polluted by infrastructure artifacts (API timeouts, incomplete responses) or annotation errors (e.g. gold answers that aren't valid API calls).

The teacher's trajectory generation depends on source type: for **QA / reasoning / math** the teacher (Claude Opus) derives the `<plan>/<route>/<obs>/<verify>/<final_answer>` trace from the question plus the dataset's own context / evidence field (Wikipedia passages for HotpotQA, search snippets for TriviaQA, the step-by-step solution for GSM8K — see § Distillation for the full evidence map); for **code (TACO) / tool use (ToolACE)** every `<route>` is executed for real (sandbox or live API) and `<obs>` carries the actual output. In both regimes the per-source verifier scores `<final_answer>` against the gold, so only gold-matching trajectories survive.

**Phase 3 — Failure-driven in-context learning.** Each failed teacher trajectory is fed (full execution trace) into GPT-4o, which classifies the failure as **(i)** information loss — Orchestrator omitted critical context when delegating; **(ii)** premature aggregation — intermediate result returned without final computation; **(iii)** format mismatch — semantically correct but wrong output shape; or **(iv)** delegation scope error — under- or over-decomposed. Each high-frequency category yields one task-agnostic constraint added to the Orchestrator's instruction. The loop runs for 3 rounds; by round 3 residual errors are routing-policy issues (wrong model picked for the task), indicating prompt clarity has saturated and further gains require Router model improvements rather than more prompt patches.

**Phase 4 — Rejection-sampled augmentation** (`scripts/data/augment_sft.py`). K = 2 extra teacher rollouts at temperatures {0.5, 1.0} per SFT question; K = 3 at {0.3, 0.7, 1.0} for the harder RL-pool questions. Only trajectories that pass the per-source verifier survive — the gold label doubles as a consistency gate.

**Phase 5 — Fallback distillation cascade** (`scripts/data/rescue_rl_pool.py`). RL-pool questions where the primary teacher (qwen3.5-plus) failed are retried under a stronger cascade (gemini-2.5-pro → claude-sonnet-4-6 → gpt-5.4) at pass@3; whichever cascade step solves the task promotes that trajectory from the RL pool into SFT. This pass shrinks the RL pool from 4,549 → **2,976** tasks (−34.6%) by rescuing 295 previously unsolvable questions; the largest gains are on tool orchestration, where gemini-2.5-pro's code generation resolves TACO tasks qwen3.5-plus could not.

Every row carries `teacher` (which model produced the trajectory) and `distillation_pass` (`primary` / `augmentation` / `fallback`) alongside `source`, so trajectory provenance is fully traceable.

## Dataset Description

### 7 real-rollout benchmarks selected for analysis

For the in-depth capability-gap audit we select the seven benchmarks whose rollouts go through the pipeline end-to-end with real worker execution (GSM8K, NuminaMath, DROP, HotpotQA, MuSiQue, TACO, ToolACE):

| Capability Axis         | Benchmarks     |    Sampled |       Router OK |       SFT |   RL Pool |
| ----------------------- | -------------- | ---------: | --------------: | --------: | --------: |
| Atomic reasoning        | GSM8K          |        500 |     483 (96.6%) |        40 |        15 |
| Compositional reasoning | NuminaMath     |      1,793 |   1,191 (66.4%) |       282 |       355 |
| Knowledge retrieval     | DROP, HotpotQA |      3,808 |   2,466 (64.8%) |       554 |       551 |
| Knowledge composition   | MuSiQue        |      1,746 |     739 (42.3%) |       196 |       773 |
| Tool orchestration      | TACO, ToolACE  |      4,956 |     710 (14.3%) |     1,985 |     1,282 |
| **Total (7-bench slice)** |              | **12,803** | **5,589 (43.7%)** | **3,057** | **2,976** |

Tool orchestration receives the largest share of SFT demonstrations here (64.9%) because routing decisions on this axis involve both model selection *and* skill selection — the router must match coding tasks to code-capable models and API-calling tasks to tool-aware models. Atomic reasoning receives the smallest (1.3%) because the router already solves 96.6% of these tasks; the remaining examples serve as a negative signal teaching the router to recognize single-step questions that should *not* be decomposed. The high router-OK rate for atomic reasoning / knowledge retrieval confirms that a 7B policy can already handle single-hop factual and arithmetic questions through direct answering; the near-zero rate for tool orchestration validates our choice to treat tool selection as a learned routing problem, not a fixed heuristic.

Per-source root-cause breakdown of the 21,642 failing real-rollout trajectories is deferred to § Error Taxonomy below.


### **61,201 multi-turn ShareGPT conversations** (system → human → assistant → observation → assistant → ...). Each row:

| Field | Type | Description |
|---|---|---|
| `id` | string | stable `{source}_{row_id}` identifier |
| `source` | string | HuggingFace dataset the question came from |
| `category` | string | one of the 9 categories above |
| `question` | string | verbatim from `source` |
| `gold_answer` | string | verbatim from `source` |
| `teacher` | string | which LM produced the trajectory |
| `distillation_pass` | string | `primary` / `augmentation` / `fallback` |
| `n_plan_rounds` | int | rounds in the trajectory (1 = single round, ≥2 = multi-round) |
| `n_subtasks` | int | total `<subtask>` count |
| `conversations` | list | the ShareGPT turns |

## 🫐 Training Pipeline

End-to-end pipeline for the selective-delegation router: from raw HuggingFace datasets to a trained Router model.

```
HuggingFace Datasets (31 sources, 10 domains)
        │
        ▼
[1] scripts/data/generate_trajectories.py   Teacher distillation (API calls)
        │                
        │                
        │            
        │                
        ▼
[2] scripts/data/build_dataset.py           Build training set
        │                - Re-validates every sample
        │                - Filters (max attempts, max tokens, max routes)
        │                - Classifies trajectory behavior
        │                - Outputs train_final.parquet + train_final_stats.json
        ▼
[3] SFT Training (LlamaFactory)     Fine-tune Qwen2.5-7B-Instruct
        │                - ShareGPT multi-turn, mask non-assistant turns
        │                - 2 epochs, lr=2e-5, DeepSpeed ZeRO-3, packing on
        ▼
[4] RL Training (verl-agent, GRPO)  Cost-aware reinforcement learning
        │                - Real worker-API calls 
        │                - Per-source verifiers (math / qa / code / tool)
        │                - Terminal reward = (1−α)·correctness + α·cost_bonus
        ▼
    Router Model
```

### 🍇 Step 1: Distillation (`scripts/data/generate_trajectories.py`)

Generates SFT trajectories by calling a teacher model (claude-sonnet-4-6, claude-opus-4-6, gpt-5.4, or qwen-max) via OpenAI-compatible API.

**How it works.**
1. Loads `configs/sft/data/sft_recipe.yaml` — 31 datasets across 10 domains with per-dataset sample counts.
2. For each dataset, streams (question, gold_answer) pairs from HuggingFace.
3. Extracts real evidence from dataset fields when available (e.g. Wikipedia context for HotpotQA, search results for TriviaQA, step-by-step solutions for GSM8K).
4. Injects evidence into the teacher prompt so the teacher writes obs based on real data.
5. Teacher generates a full trajectory: `<plan>` → `<route>` → `<obs>` → `<verify>` → `<final_answer>`.
6. `validate_schema.py` validates against the 16 schema rules (exactly one `<final_answer>`, strictly increasing rounds, DAG `depends_on`, closed-vocab model+skill, etc.).
7. Valid samples appended to output JSONL; invalid go to `_failed.jsonl` for later inspection.
8. Resume is automatic: reads existing output file IDs and skips already-generated samples.

**Usage.**
```bash
export API_KEY="sk-..."
export HF_TOKEN="hf_..."
export HF_ENDPOINT="https://hf-mirror.com"         # optional China mirror

# Full distillation (all 31 datasets)
python3 scripts/data/generate_trajectories.py --full --concurrency 200 --out-name phase_c_final

# Single dataset
python3 scripts/data/generate_trajectories.py --only hotpotqa_fullwiki --n 100

# Alternate endpoint / teacher
python3 scripts/data/generate_trajectories.py --full --concurrency 1000 \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --api-key sk-...  --recipe configs/sft/data/sft_recipe_qwen.yaml
```

**Evidence extraction.** Wherever datasets carry a gold evidence field, the distiller injects it into the teacher prompt so obs are factual rather than hallucinated:

| Source | Evidence field | Example |
|---|---|---|
| hotpotqa_fullwiki | `context` (Wikipedia passages) | `[Title] sentence1 sentence2 ...` |
| 2wikimultihopqa | `evidences` | Supporting fact sentences |
| musique_answerable | `paragraphs` | Paragraph texts |
| strategyqa | `evidence` | Evidence list |
| triviaqa (rc split) | `search_results.search_context` | Web-search snippets |
| gsm8k | `answer` | Step-by-step solution |
| hendrycks_math | `solution` | LaTeX solution |
| codeforces_cots | `editorial` | Problem editorial |
| sciq | `support` | Supporting passage |
| logiqa2 / folio / bbh | `text` / `premises` / `input` | Problem context |
| quality | `article` | Full article (truncated 3 k chars) |

Datasets without evidence (nq_open, webquestions, arc, mmlu, commonsenseqa, piqa, social_iqa, winogrande) rely on the teacher's own knowledge; the resulting obs quality is slightly lower but acceptable.

**Output format.** Each line in the JSONL is a complete trajectory:
```json
{
  "id": "hotpotqa_fullwiki_93331229",
  "source": "hotpotqa_fullwiki",
  "domain": "multihop_qa",
  "behavior": "oneshot",
  "teacher": "claude-sonnet-4-6",
  "messages": [
    {"role": "system",    "content": "You are generating ONE training trajectory ..."},
    {"role": "user",      "content": "Question: ...\nCorrect answer: ...\nREAL EVIDENCE: ..."},
    {"role": "assistant", "content": "<plan round=\"1\">... <route ...>...</route>"},
    {"role": "tool",      "content": "<obs subtask=\"1\">...</obs>"},
    {"role": "assistant", "content": "<verify ...>...<final_answer>...</final_answer>"}
  ],
  "gold": "correct answer",
  "valid": true,
  "stats": {"is_lazy": false, "n_plan_rounds": 1, "n_routes": 2}
}
```

**Trajectory behaviors (4 types).**
- **lazy** (15.6%): direct answer without decomposition. Teaches the router when NOT to delegate.
- **oneshot** (49.5%): single-round `plan → route → obs → verify → answer`. Clean parallel decomposition.
- **continuation** (30.4%): multi-round. Round 1 explores, round 2+ plans based on round-1 obs.
- **decomp_repair** (4.4%): verify detects issues, triggers a re-plan with targeted repair.

### 🍇 Step 2: Build Training Set (`scripts/data/build_dataset.py`)

Converts raw JSONL to validated parquet.

```bash
python3 scripts/data/build_dataset.py \
  --inputs data/sft/phase_c_final.jsonl \
  --snapshot phase_c_final
```

Applies: schema re-validation; filter rules (max attempts / tokens / routes); behavior classification; outputs `train_final.parquet` + `train_final_stats.json`.

**Quality audit (`scripts/data/audit_quality.py`)**
```bash
python3 scripts/data/audit_quality.py data/sft/phase_c_final.jsonl --verbose
```
Checks: schema validation, message structure, obs quality, gold match, duplicates, domain coverage.

### 🥝 Step 3: SFT Training

**Data.** The 61,201 trajectories produced by the pipeline above are released as the `sft_full` split of [tinaxie/Uno-Curriculum](https://huggingface.co/datasets/tinaxie/Uno-Curriculum) on Hugging Face.

**Config.**
- Base: Qwen2.5-7B-Instruct, full FT
- DeepSpeed ZeRO-3, bf16, packing on, cutoff 16,384
- 2 epochs, lr 2e-5, cosine, warmup 100 steps
- Effective batch 128 (4 × per_dev 1 × grad_accum 32)
- Reference run on 4× H100 80GB: 246 steps, ~6h14m, train_loss 0.5875, eval_loss 0.2427 (1% holdout). Launch via `bash scripts/sft/run_sft.sh`.
  
#### 🍏 Hierarchical SFT — Methodology

For every question that survives the three-stage curriculum filter (§ Data Selection Pipeline), the teacher produces an expert trajectory

$$
\tau^{\star} = (q,\, a_1^{\star},\, o_1,\, a_2^{\star},\, o_2,\, \ldots,\, a_T^{\star}),
$$

where $q$ is the user question, $a_t^{\star}$ is an expert Orchestrator action at step $t$, and $o_t$ is the observation returned by the dispatched workers. 61,201 such trajectories make up the training corpus.

Every Orchestrator action is a **two-stage decision** — **Stage 1 (decomposition)**: emit a plan commitment $P_t$ consisting of a set of subtasks with `depends_on` DAG edges; **Stage 2 (routing)**: for each subtask in $P_t$, emit a routing commitment $r_{t,k}$ that dispatches it to a $(\text{worker model},\, \text{skill})$ pair. At the final step the action is a single Finish($y$) emission. Both stages are serialised into the **same** `assistant` turn under a fixed XML grammar (`<plan>…</plan>` for Stage 1, `<route …>…</route>` for each Stage 2 commitment), interleaved with `observation` turns carrying the worker returns $o_t$. We train one policy $\pi_\theta$ (Qwen2.5-7B-Instruct, full FT, DeepSpeed ZeRO-3, 2 epochs, lr 2e-5, effective batch 128) on this corpus under the standard causal-LM next-token objective — **both stages share the same parameters and are optimised jointly in one backward pass**.

**Token-level loss mask.** Every token of every `assistant` turn is a prediction target and contributes to the loss. Every token of every `observation` turn is masked out via `observation_tag: observation` in the LlamaFactory yaml: observations are environment signals, not policy outputs, and must not carry gradient into $\theta$. The system prompt and the user question are masked by default, as in any instruction-tuning recipe. No custom loss-splitting, auxiliary head, or per-action weighting is introduced.

#### 🍏 Hierarchical SFT — Theoretical support

Given expert orchestration trajectories $\{(s_t, a_t^{\star})\}$, where $s_t$ collects the question and all prior actions and observations, we finetune $\pi_\theta$ by behaviour cloning:

$$
\theta^{\star} = \mathop{\mathrm{arg\,max}}\limits_{\theta} \sum_{\tau^{\star}} \sum_{t=1}^{T} \log \pi_\theta(a_t^{\star} \mid s_t). \qquad (1)
$$

In our setting the worker models are frozen and their outputs enter only through the observations $o_t$, so the full trajectory likelihood decomposes into a policy factor and an environment factor:

$$
\log p(\tau^{\star} \mid q) = \underbrace{\sum_{t=1}^{T} \log \pi_\theta(a_t^{\star} \mid s_t)}_{\text{policy factor (SFT objective)}} + \underbrace{\sum_{t=1}^{T} \log p(o_t \mid s_t, a_t^{\star})}_{\text{environment factor, constant in }\theta}. \qquad (2)
$$

Maximising the full likelihood in $\theta$ therefore reduces to the policy factor alone. At the token level, dropping the environment factor is exactly the mask that excludes `observation` tokens from the cross-entropy loss; the `assistant` loss of Equation (1) is what the training run actually computes.

Each expert action $a_t^{\star}$ is itself compound: it first commits to a decomposition $P_t$ (**Stage 1**) and then, conditional on that decomposition, commits to a sequence of routing decisions $r_{t,1}, \ldots, r_{t,K_t}$ (**Stage 2**), all within the same `assistant` turn. Under the causal-LM parameterisation this joint conditional factorises exactly as:

$$
\pi_\theta(a_t \mid s_t) = \underbrace{\pi_\theta(P_t \mid s_t)}_{\text{Stage 1: decomposition}} \cdot \underbrace{\prod_{k=1}^{K_t} \pi_\theta\bigl(r_{t,k} \mid s_t,\, P_t,\, r_{t,1}, \ldots, r_{t,k-1}\bigr)}_{\text{Stage 2: routing (one factor per subtask)}}. \qquad (3)
$$

Equation (3) makes the **two-stage structure** of the policy explicit: the first factor is the decomposition distribution $\pi_\theta^{\text{plan}}(P_t \mid s_t)$; the product is the routing distribution $\pi_\theta^{\text{route}}(r_{t,k} \mid s_t, P_t, r_{t,<k})$, which is conditioned on the already-committed plan by virtue of left-to-right causal masking. Although $\pi_\theta^{\text{plan}}$ and $\pi_\theta^{\text{route}}$ share every parameter in $\theta$, they are *conditionally separated at the token level* — no route token can attend forward to a later route, and no plan token can attend forward to any route. This is how we obtain the hierarchical "decompose, then route" structure of a dual-policy orchestrator *from a single model, a single corpus, and a single SFT run*, without an auxiliary head, a separate decomposer network, or a per-stage loss weight.

**Reference SFTTrainer launch (legacy backbone-only path).**
```python
# train_sft.py — torchrun --nproc_per_node=8 train_sft.py
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import SFTTrainer, SFTConfig
import json

dataset = load_dataset("parquet", data_files="/home/xieht/data/sft/train_final.parquet", split="train")
def parse_messages(x):
    m = x["messages"]
    if isinstance(m, str): m = json.loads(m)
    x["messages"] = m; return x
dataset = dataset.map(parse_messages).train_test_split(test_size=0.02, seed=42)

model_name = "Qwen/Qwen2.5-7B-Instruct"
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
model     = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="bfloat16", trust_remote_code=True)
args = SFTConfig(
    output_dir="/home/xieht/data/sft/checkpoints",
    num_train_epochs=3, per_device_train_batch_size=2,
    gradient_accumulation_steps=8,       # effective batch = 2 * 8 * 8 GPUs = 128
    learning_rate=2e-5, lr_scheduler_type="cosine", warmup_ratio=0.05,
    weight_decay=0.01, max_seq_length=4096, bf16=True,
    logging_steps=10, save_steps=500, eval_strategy="steps", eval_steps=500,
    save_total_limit=3, deepspeed="ds_config_zero2.json",
    dataloader_num_workers=4, remove_unused_columns=False,
)
trainer = SFTTrainer(model=model, args=args,
                     train_dataset=dataset["train"], eval_dataset=dataset["test"],
                     processing_class=tokenizer)
trainer.train(); trainer.save_model("/home/xieht/data/sft/router_final")
```

### 🥥 Step 4: RL Training (`scripts/rl/`)

**Driver — GRPO.** The launcher + multi-turn generation manager live under `scripts/rl/` (undergoing a rewrite against vllm 0.11.x; see `verl/third_party/vllm/vllm_v_0_11_0/` and the step-gated smoke in `scripts/rl/smoke_vllm_step1.py`). The generation manager splices `<obs>` content inline via chat-template role switches, keeping the RL-time token stream byte-identical to SFT.

**Rollout structure.** The router operates as an iterative delegator. At each turn it may emit a batch of parallel subtasks together with an explicit `(model, skill)` choice per subtask; worker responses are returned synchronously via an `<obs>` block and the router autoregressively decides, on the next turn, whether to issue another round of delegation or terminate with a final answer. Workers are treated as stateless oracles rather than learnable sub-agents — this keeps credit assignment tractable under group-relative policy optimisation while preserving the wide-parallel-with-feedback structure that motivates hierarchical routing.

**Reward** — terminal only:
- format invalid → 0.0
- valid mid-step → 0.0
- terminal & wrong → 0.0
- terminal & correct → $(1-\alpha)\cdot 1 + \alpha\cdot R_{\text{cost}}$, $\alpha=0.1$. $R_{\text{cost}}\in[0,1]$ is a rolling-percentile normalised cost reward: $R_{\text{cost}} = 1 - \mathrm{clip}\!\big((\sqrt{c} - p_{5}) / (p_{95}-p_{5}),\,0,\,1\big)$ over a 1000-episode rolling buffer, so a single Opus outlier can't saturate the signal and no budget-cap magic number needs tuning.

Worker calls are issued through an OpenAI-compatible HTTPS endpoint configured by the `REMOTE_API_BASE` / `REMOTE_API_KEY` environment variables (`agent_system/environments/env_package/uno/envs.py`), so the same rollout code can target any provider gateway. The router's `(model, skill)` choice is forwarded verbatim — no tier remap — so the **routed** model is the model that actually runs and cost / quality differences are authentic. Token usage is read from the API response for cost accounting; per-model `max_tokens` caps bound the episodic cost; a hard per-episode USD ceiling early-terminates any runaway rollout.

#### 🍏 Rollout and reward placement

The RL phase optimises the same policy $\pi_\theta$ initialised from SFT, on the disjoint pool of $\sim$2,976 questions where the teacher cascade itself failed (§ Phase 5) — i.e. exactly the regime where behaviour cloning can no longer provide a target trajectory and exploration becomes necessary. The objective is **cost-aware group-relative policy optimisation** under the schema-v1.1 grammar already established at SFT time (`<plan>/<route>/<obs>/<verify>/<final_answer>`), so the RL-time token stream is byte-identical to the SFT corpus and no distribution shift is induced by the grammar itself. The agent loop and the reward manager are implemented as side-effect-imported plug-ins to upstream verl v0.7.0 (`scripts/rl/uno_rollout.py`, `scripts/rl/uno_reward.py`); no modification to the trainer kernel is required.

A rollout is a *multi-turn* trajectory of up to $T = 5$ assistant turns interleaved with environment-injected observations. At turn $t$ the policy emits a `<plan>` block followed by one or more `<route>` commitments; the environment dispatches each `<route>` to a real worker model–skill pair via the OpenAI-compatible HTTPS gateway, blocks for the response, and re-injects the concatenated `<obs>` block as a `tool`-role chat turn (apply_chat_template with `remove_system_prompt=True` and a single-byte `\n` guard before the splice, reproducing LlamaFactory's `format_observation` exactly). The trajectory terminates when the policy emits `<final_answer>...</final_answer>`, when the cumulative response length exceeds the rollout budget $L = 16{,}384$, or when $T$ assistant rounds are exhausted. Per-source verifiers (math equivalence, QA EM/F1, code unit-test execution against TACO/codecontests stdin/stdout tests, ToolACE schema match) score the final answer to produce a binary correctness signal $c \in \{0, 1\}$.

For each prompt we draw $G = 8$ independent rollouts under the current policy. Verl's agent-loop runtime (`AgentLoopWorker × G` against a shared async vLLM server) executes them concurrently; each worker issues sub-agent HTTP calls through `loop.run_in_executor(...)` so the policy server's event loop is never blocked by remote-API latency. This is what makes multi-turn rollout tractable at $G \times B$ trajectories per step (where $B$ is the prompt batch size) on a single node.

#### 🍏 Group-relative objective

Let a trajectory be $\tau = (q,\, a_1,\, o_1,\, a_2,\, o_2, \ldots,\, a_{T'})$ where $q$ is the question, $a_t$ is the policy-emitted assistant turn, $o_t$ is the worker-returned observation, and $T' \le T$ is the realised number of rounds. Tokenise $\tau$ into a single sequence and let $m_{i,t} \in \{0, 1\}$ be a **policy mask** that is 1 on every token emitted by $\pi_\theta$ and 0 on every observation token and every chat-template control token. A trajectory-level scalar reward

$$
R(\tau) = c \cdot \big[(1-\alpha) + \alpha \cdot R_{\text{cost}}(\tau)\big], \quad \alpha = 0.1. \qquad (4)
$$

is placed at the **last policy token** of $\tau$ — at index $t^{\star} = \max\{t : m_{i,t} = 1\}$ — yielding a token-level reward tensor $r_{i,t}$ that is zero everywhere except at $t^{\star}$. Placement on the last *valid* token (the standard outcome-RM convention) would land inside an `<obs>` span whenever a rollout is truncated mid-route, since `response_mask` is interleaved $1{\cdot}1{\cdots}0{\cdot}0{\cdots}1{\cdot}1{\cdots}$ across alternating policy / observation turns; the policy-mask convention guarantees the gradient lands on a token $\pi_\theta$ actually emitted (`scripts/rl/uno_reward.py:107`).

For each question $q$ we form the group $\mathcal{G}_q = \{\tau_1, \ldots, \tau_G\}$ of its $G = 8$ rollouts. The **group-relative advantage** for the $i$-th rollout is

$$
\hat{A}_i = \frac{R(\tau_i) - \mu_q}{\sigma_q + \varepsilon}, \quad \mu_q = \tfrac{1}{G}\sum_{j \in \mathcal{G}_q} R(\tau_j), \quad \sigma_q = \mathrm{std}_{\mathcal{G}_q}\,R(\tau_j). \qquad (5)
$$

and is broadcast token-wise as $\hat{A}_{i,t} = \hat{A}_{i} \cdot m_{i,t}$. Equation (5) is the GRPO baseline of [Shao et al., 2024]: the within-group mean replaces a learned critic, and the within-group standard deviation rescales the advantage so updates remain bounded under reward-distribution shift across questions of heterogeneous difficulty (math vs. multi-hop QA vs. tool orchestration). Crucially, when an entire group fails ($\mu_q = 0,\,\sigma_q = 0$) the advantage is exactly zero and the policy receives no spurious signal from prompts it cannot yet solve — the ill-posed credit-assignment problem on hopeless questions is structurally avoided rather than heuristically masked.

The actor objective is the standard PPO surrogate restricted to policy tokens, with an additive low-variance KL regulariser to a frozen reference policy $\pi_{\text{ref}}$ (the SFT checkpoint):

$$
\mathcal{L}(\theta) = -\,\mathbb{E}_{\tau \sim \pi_{\theta_{\text{old}}}}\!\left[\sum_{i,t} m_{i,t}\, \min\!\Big(\rho_{i,t}\,\hat{A}_{i,t},\; \mathrm{clip}(\rho_{i,t}, 1-\epsilon, 1+\epsilon)\,\hat{A}_{i,t}\Big)\right] + \beta\,\hat{D}_{\text{KL}}\!\big[\pi_\theta \,\Vert\, \pi_{\text{ref}}\big]. \qquad (6)
$$

where $\rho_{i,t} = \pi_\theta(x_{i,t}\mid s_{i,t}) / \pi_{\theta_{\text{old}}}(x_{i,t}\mid s_{i,t})$ is the per-token importance ratio, $\hat{D}_{\text{KL}} = \rho - \log\rho - 1$ is the unbiased low-variance KL estimator of [Schulman, 2020] (`kl_loss_type=low_var_kl`), and $(\beta,\,\epsilon) = (10^{-3},\, 0.2)$. Three design choices follow from the multi-turn structure of $\tau$:

- **Token-level masking under (6) is identical to SFT's.** The mask $m_{i,t}$ in (6) is the same mask used at SFT time (§ Hierarchical SFT — Theoretical support): observations carry no gradient. This makes the RL update *consistent* with the SFT update on the same token positions — the policy is never penalised for content it did not author, and SFT $\to$ RL transfer reduces to a strict change of objective on a fixed set of trainable positions, with no spurious gradient leakage at turn boundaries.
- **KL is an additive loss, not a per-step reward shaper.** We set `algorithm.use_kl_in_reward=False` and `actor.use_kl_loss=True`. Folding KL into the per-step reward would interact with the group-relative normalisation in (5) and re-scale the implicit baseline by reference-policy disagreement at every token, coupling regularisation strength to advantage magnitude in a way that obscures the cost-reward signal. Keeping KL as an additive term in (6) decouples the two.
- **No entropy bonus.** With $\alpha = 0.1$ and a $62.5\times$ price spread across the worker pool (§ Worker Pool), exploration is already driven by the cost-reward gradient: cheaper-but-correct rollouts in $\mathcal{G}_q$ produce higher $R(\tau)$ and therefore positive $\hat{A}$, which encourages diversifying across $(\text{model}, \text{skill})$ pairs without an explicit entropy term that would otherwise compete with the cost objective.

The cost component $R_{\text{cost}}(\tau) \in [0, 1]$ is the rolling-percentile normalised cost reward of § RL Training. The blend in (4) is **multiplicative in correctness** ($c \cdot R_{\text{cost}}$, not $c + R_{\text{cost}}$): incorrect rollouts receive zero credit regardless of their cost, which forecloses the failure mode where the router collapses to `direct_answer` on hard questions to harvest a cheap-and-wrong cost bonus. Conversely, when $c = 1$ the $\alpha = 0.1$ blend ensures cheaper-but-correct trajectories dominate within their group, which is the cost-aware preference we wish to instil.

**Hyperparameters.** $G = 8$ (`actor_rollout_ref.rollout.n`), $T = 5$ (`multi_turn.max_assistant_turns`), $L = 16{,}384$ (`data.max_response_length`), prompt cap $4096$ (`data.max_prompt_length`), $\alpha = 0.1$ (cost blend, `multi_turn.alpha`), $\beta = 10^{-3}$ (`actor.kl_loss_coef`), $\epsilon = 0.2$ (PPO clip, default), AdamW $\eta = 10^{-6}$ (`actor.optim.lr`), dynamic batch size with $24{,}000$ tokens / GPU cap (`actor.use_dynamic_bsz=True`, `ppo_max_token_len_per_gpu`). FSDP ZeRO-3 with parameter and optimiser offload (`fsdp_config.param_offload=True`, `optimizer_offload=True`), vLLM 0.11 rollout server with $\text{TP}=1$, $0.6$ GPU memory utilisation, and `enforce_eager=True` (vLLM 0.11 cudagraph + `free_cache_engine` exhibits an intermittent illegal-memory-access on cross-step rebuild; eager mode incurs a $\sim 22\%$ throughput penalty but is stable). Reference single-node 4×H100 throughput: $\sim 270$ s/step under cudagraph, $\sim 340$ s/step under eager.

## 🎻 Worker Pool

The router dispatches every subtask to a `(model, skill)` pair drawn from a closed vocabulary of **8 worker models** and **13 skills** (`configs/pools.yaml`). The closed-vocabulary constraint keeps the action space discrete and tractable for GRPO; the heterogeneous pricing structure is what gives the cost-aware reward a non-trivial signal to optimise.

| Model | $/M input | $/M output | Allowed skills |
|---|--:|--:|---|
| `gemini-2.5-flash-lite` | 0.10 | 0.40 | direct_answer, web_search, read_document, extract_field |
| `gemini-2.5-flash` | 0.30 | 2.50 | direct_answer, web_search, read_document, extract_field |
| `gemini-3-flash-preview` | 0.50 | 3.00 | all 13 skills |
| `kimi-k2.5` | 0.60 | 3.00 | direct_answer, reason, web_search, read_document, extract_field, fact_check |
| `gpt-5.3-codex` | 1.75 | 14.00 | direct_answer, symbolic_math, execute_python, execute_shell, call_api, read_code, parse_structured |
| `gpt-5.4` | 2.50 | 15.00 | all 13 skills |
| `claude-sonnet-4-6` | 3.00 | 15.00 | all 13 skills |
| `claude-opus-4-6` | 5.00 | 25.00 | all 13 skills |

**Cost spread.** Output prices span \$0.40 / M tokens (`gemini-2.5-flash-lite`) to \$25.00 / M tokens (`claude-opus-4-6`) — a **62.5×** ratio ($25.00 / \$0.40$) between the cheapest and most expensive worker. This dynamic range matters because the rolling-percentile cost reward $R_{\text{cost}}$ (§ RL Training) reads its bounds from the empirical $p_5$ / $p_{95}$ of the past 1,000 episodes: with a 62.5× spread the percentile gap is wide relative to per-call token noise, so a single Opus outlier cannot saturate $R_{\text{cost}}$ to 0 and a single Flash-lite call cannot push it to 1. A pool with a < 5× spread, by contrast, would collapse $R_{\text{cost}}$ into noise and the cost-aware blend at $\alpha = 0.1$ would degenerate to a pure-correctness reward.

**Skills (13 total),** grouped by routing semantics:

| Group | Skills |
|---|---|
| Answer & reason | `direct_answer`, `reason` |
| Retrieve | `web_search`, `database_query`, `fact_check` |
| Read & extract | `read_document`, `read_code`, `extract_field`, `parse_structured` |
| Execute | `execute_python`, `execute_shell`, `call_api` |
| Symbolic | `symbolic_math` |

The model–skill bipartite graph is intentionally **sparse**: roughly 60 valid `(model, skill)` pairs out of the dense 8 × 13 = 104, because cheap models advertise only the skills on which they are competitive. This sparsity prunes routing decisions that are trivially wrong (e.g. `symbolic_math` on `gemini-2.5-flash-lite`) before learning begins, and shrinks the effective action space the router has to explore under GRPO.

**Pool ablations** (`pool_ablations:` in `configs/pools.yaml`) — four pre-defined sub-pools used in the ablation study:

| Pool | Composition | What it isolates |
|---|---|---|
| `no_frontier` | drops `claude-opus-4-6`, `gpt-5.4`, `claude-sonnet-4-6` | router behaviour when no frontier worker is reachable |
| `minimal` | `gemini-2.5-flash`, `gemini-3-flash-preview`, `gpt-5.4` | minimum pool size at which routing is non-trivial |
| `mid_only` | `gemini-3-flash-preview`, `gpt-5.3-codex`, `claude-sonnet-4-6` | "decompose well" decoupled from "pick the strongest" |
| `pro_only` | `gpt-5.3-codex`, `gpt-5.4`, `claude-sonnet-4-6`, `claude-opus-4-6` | quality-only regime — cost reward effectively flattened |

## 🍋 Error Taxonomy

> Scope: every failing rollout in the tables below is a **real interaction-mode router trajectory** — the 7B router ran end-to-end through the pipeline (plan → route → real worker API / code executor / tool-schema call → obs → verify → final_answer), and only these execution-grounded rollouts are analysed here. Rollouts that never reached a real worker invocation (e.g. empty outputs, immediate format crashes) are excluded so each failure can be attributed to a concrete routing / worker-response interaction rather than an SFT-warmup artefact. The seven benchmarks in the audit (GSM8K, NuminaMath, DROP, HotpotQA, MuSiQue, TACO, ToolACE) also align exactly with the RL evaluation pool, so every failure mode here is a failure we can later address with RL reward shaping.

### Base router — Qwen2.5-7B

The Qwen2.5-7B router solves 43.7% of the 12,803 sampled tasks under pass@3; the remaining 7,214 tasks yield **21,642 failing rollouts** that we classify by root cause. Success varies sharply across capability axes:

| Capability Axis         | Router Success Rate |
| ----------------------- | ------------------: |
| Atomic reasoning        |               96.6% |
| Compositional reasoning |               66.4% |
| Knowledge retrieval     |               64.8% |
| Knowledge composition   |               42.3% |
| Tool orchestration      |               14.3% |

Roughly three-quarters of failures are content errors and the remaining quarter are protocol errors, all observed under the same source-aware planner prompt the teacher uses (so failures reflect capability gaps, not prompting choices). Root-cause distribution over the 21,642 failing rollouts:

| Root Cause                  |  Count | Share | Description                                                              |
| --------------------------- | -----: | ----: | ------------------------------------------------------------------------ |
| Output not code             |  7,417 | 34.2% | Returned numeric, natural-language, or skeleton output instead of code   |
| Wrong entity                |  4,887 | 22.6% | Retrieved or reasoned to an incorrect entity (QA tasks)                  |
| No finish / incomplete      |  2,932 | 13.5% | Trajectory terminated without a `finish()` call                          |
| No tool call                |  1,230 |  5.7% | ToolACE rollout answered without issuing any tool call                   |
| Numeric reasoning error     |  1,221 |  5.6% | Incorrect arithmetic over passages or math prompts                       |
| Format/reasoning error      |    746 |  3.4% | Mathematically inequivalent answer, wrong interval, wrong MC letter      |
| Partial QA overlap          |    508 |  2.3% | Answer overlaps with gold but verifier rejects it                        |
| NL instead of API call      |    454 |  2.1% | Prose description instead of a structured function call                  |
| Close numeric miss          |    400 |  1.8% | Within 10% of gold but not accepted                                      |
| Loop / stall                |    376 |  1.7% | Repeated identical tool calls until the step budget runs out             |
| Empty answer                |    351 |  1.6% | `finish("")` with an empty payload                                       |
| Wrong code logic            |     94 |  0.4% | Structurally complete code with an incorrect algorithm                   |
| Other                       |  1,426 |  6.6% | Refusals, context hints, wrong API function, rounding, etc.              |

The two dominant failure modes — *output not code* (34.2%) and *wrong entity* (22.6%) — together account for 57% of failing rollouts. Both are *delegation failures*: the router either sends a competitive-programming task to a model that summarizes in prose or handles multi-hop QA without routing to search-capable workers. *No finish / incomplete* and *no tool call* (19.2% combined) are protocol failures concentrated almost entirely on tool orchestration.

### Per-source breakdown

**GSM8K — 51 failure rollouts (Router success 96.6%).** Pure arithmetic capability limitation. The router correctly identifies these single-step tasks as not requiring decomposition.

| Root Cause          | Count | Share |
| ------------------- | ----: | ----: |
| `calculation_error` |    33 | 64.7% |
| `rounding_precision`|    15 | 29.4% |
| `off_by_10x`        |     3 |  5.9% |

**NuminaMath — 1,806 failure rollouts (Router success 66.4%).** Competition-level mathematics; the router produces a mathematically inequivalent answer (different interval notation, unsimplified fractions, wrong MC letter) or a plain calculation error. Format mismatches (~35%) can be closed with better normalization; the remainder requires capability.

| Root Cause                  | Count | Share |
| --------------------------- | ----: | ----: |
| `wrong_answer_calculation`  |   778 | 43.1% |
| `wrong_answer_math_form`    |   639 | 35.4% |
| `wrong_answer_choice_letter`|   107 |  5.9% |
| `wrong_answer_rounding`     |   100 |  5.5% |
| `wrong_answer_off_by_2x`    |    82 |  4.5% |
| `wrong_answer_zero`         |    34 |  1.9% |
| Other                       |    66 |  3.7% |

**DROP — 1,650 failure rollouts (Router success 69.4%).** Balanced between entity-extraction errors and numeric reasoning over passages.

| Root Cause                     | Count | Share |
| ------------------------------ | ----: | ----: |
| `wrong_answer_wrong_entity`    |   768 | 46.5% |
| `wrong_answer_numeric_far`     |   635 | 38.5% |
| `empty_answer`                 |   126 |  7.6% |
| `wrong_answer_numeric_close`   |    78 |  4.7% |
| `wrong_answer_partial_overlap` |    26 |  1.6% |
| Other                          |    17 |  1.0% |

**HotpotQA — 2,376 failure rollouts (Router success 60.6%).** Overwhelmingly wrong-entity errors: the 2-hop structure means a wrong first hop cascades into a wrong second hop.

| Root Cause                     | Count | Share |
| ------------------------------ | ----: | ----: |
| `wrong_answer_wrong_entity`    | 1,785 | 75.1% |
| `wrong_answer_partial_overlap` |   222 |  9.3% |
| `empty_answer`                 |   193 |  8.1% |
| `wrong_answer_numeric_close`   |    91 |  3.8% |
| `wrong_answer_numeric_far`     |    61 |  2.6% |
| Other                          |    24 |  1.0% |

**MuSiQue — 3,021 failure rollouts (Router success 42.3%).** Hardest QA source. 3–4-hop structure compounds wrong-entity rates multiplicatively; the router has no mechanism to verify intermediate hops before routing the next sub-query.

| Root Cause                     | Count | Share |
| ------------------------------ | ----: | ----: |
| `wrong_answer_wrong_entity`    | 2,334 | 77.3% |
| `wrong_answer_partial_overlap` |   260 |  8.6% |
| `wrong_answer_numeric_close`   |   231 |  7.6% |
| `wrong_answer_numeric_far`     |   125 |  4.1% |
| Other                          |    71 |  2.4% |

**TACO — 8,013 failure rollouts (Router success 15.4%).** 93% of failures produce non-code output: the router answers in prose or a plain number rather than delegating to a code-generation specialist. Only 1.2% are genuine algorithmic failures — a pure delegation-strategy gap.

| Root Cause                         | Count | Share |
| ---------------------------------- | ----: | ----: |
| `wrong_answer_numeric_not_code`    | 4,121 | 51.4% |
| `wrong_answer_not_code`            | 3,296 | 41.1% |
| `loop_or_stall`                    |   205 |  2.6% |
| `wrong_answer_trivial_code`        |   135 |  1.7% |
| `no_finish_or_incomplete`          |   119 |  1.5% |
| `wrong_answer_code_logic`          |    94 |  1.2% |
| Other                              |    43 |  0.5% |

**ToolACE — 4,725 failure rollouts (Router success 12.5%).** 84% of failures are protocol violations — the router either issues no tool call or never reaches a `finish()` within the step budget. Another 10% issue a natural-language description of the desired call instead of the structured call itself. These patterns are what SFT should close first, before any routing-quality optimization is meaningful on this source.

| Root Cause                          | Count | Share |
| ----------------------------------- | ----: | ----: |
| `no_finish_or_incomplete`           | 2,774 | 58.7% |
| `no_tool_call_in_answer`            | 1,230 | 26.0% |
| `wrong_answer_nl_instead_of_tool`   |   454 |  9.6% |
| `loop_or_stall`                     |   154 |  3.3% |
| `wrong_tool_completely`             |    35 |  0.7% |
| `refusal_cannot_execute`            |    19 |  0.4% |
| Other                               |    59 |  1.3% |

### Model comparison — Qwen3-4B vs Qwen2.5-7B

We also evaluate a smaller router (Qwen3-4B-Instruct) on the same pipeline. Its failure profile differs qualitatively:

| Failure Mode                         | Qwen2.5-7B | Qwen3-4B |
| ------------------------------------ | ---------: | -------: |
| Protocol failure (no finish / empty) |      22.8% |    98.1% |
| Wrong-answer content error           |      76.9% |     1.9% |
| Missing context / refusal            |       0.2% |     0.0% |

The 7B router's failures are dominated by *capability* limitations (wrong answers), while the 4B router fails almost exclusively at *protocol compliance* (unable to produce valid tool calls or a terminal finish action). This suggests protocol-following ability is a prerequisite that emerges between 4B and 7B scale: SFT for the 4B model should prioritize format compliance before routing quality, whereas SFT for the 7B model can target content-level delegation decisions directly.

## 🧪 Evaluation

A unified eval pipeline (`eval_pipeline/`) drives any router against any benchmark via a uniform `(question, gold, verify_fn)` adapter. Coverage spans **13 benchmarks** organised along the same capability axes used to construct the training corpus, so generalisation can be read axis-by-axis rather than as a single aggregate score.

| Capability axis | Benchmarks | Verifier |
|---|---|---|
| **Agentic / SWE** | SWE-bench (500 instances), Terminal-Bench (89 tasks) | `swebench.harness.run_evaluation` Docker apply + test suite; Harbor Docker container per task with `test.sh` |
| **Generalist agent** | GAIA | per-task answer normalisation |
| **Tool use** | ToolBench, ToolACE (held-out) | tool-call schema match + gold-trace match |
| **Code** | HumanEval, MBPP, LiveCodeBench | unit tests in sandbox |
| **Math** | GSM8K, MATH, AIME | symbolic equivalence + numeric tolerance |
| **Knowledge / reasoning** | MMLU, GPQA | exact match / multiple-choice |
| **Multi-hop QA** | DROP, HotpotQA, MuSiQue | per-source verifier (EM / F1 / numeric) |
| **Long context** | MRCR | retrieval-aware match |

All 13 adapters live under `eval_pipeline/benchmarks/` and share the same router-agnostic interface.

```bash
python -m eval_pipeline.run --router ROUTER --bench BENCH --api_key KEY

# Routers:    uno-sft, uno-rl, direct, random,
#             oracle-cheapest, oracle-codex, router+claude, router-r1
```

### Baselines

| System | Type | Description |
|--------|------|-------------|
| GPT-5.4 direct | No routing | Strongest single-model upper bound |
| Cheapest-always | Fixed routing | Always pick cheapest (gemini-2.5-flash-lite) |
| Strongest-always | Fixed routing | Always pick most expensive (claude-opus-4-6) |
| Router+Claude | Decomposition only | Our decomposer + frontier executor |
| Random | Random routing | Uniform over valid pairs |
| Router-R1 | Prior art | Search/QA-only, no cost reward |
| **Uno-SFT** | **Learned routing + decomposition** | Our method (SFT only) |
| **Uno-RL** | **Learned routing + decomposition + RL** | Our full method |
....



## 🧩 Repository Structure

```
multiagentRL/
  configs/
    pools.yaml                 # Worker pool: 10 models × 13 skills
    sft/                       # SFT training configs
  docs/
    case_studies/              # Worked examples (fix-git, subtask-conflict)
  scripts/
    data/                      # Teacher distillation scripts
    sft/                       # SFT training scripts
    rl/                        # GRPO RL training scripts
  eval_pipeline/
    config.py                  # Model pool, costs, skills
    run.py                     # Main entry point
    routers/                   # Router adapters
    benchmarks/                # Benchmark adapters (swebench, terminalbench, ...)
  agent_system/
    environments/              # RL environment with real API sub-agents
```

## 🚀 Quick Start

```bash
# Evaluation
python -m eval_pipeline.run --router uno-sft --bench swebench \
    --local_base http://localhost:8000/v1 --local_model Uno-SFT --api_key KEY

# Training
python scripts/data/generate_trajectories.py --full --concurrency 200    # Distillation
bash scripts/sft/run_sft.sh                                              # SFT
bash scripts/rl/run_grpo_uno.sh                                          # RL
```

## Acknowledgements

Our training experiments are powered by our heavily modified fork of [verl](https://github.com/volcengine/verl), an open-source RLHF library.
