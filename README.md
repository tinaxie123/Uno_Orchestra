# 🎉 Uno-Orchestra

> We propose **Uno-Orchestra** — a 7b router model that, given a task, decomposes it into subtasks and dispatches each to a `(worker model, skill)` pair.
>
> 💗 Uno-Orchestra is trained in two stages：
> 📚 **SFT** on distilled trajectories
>  💰 **cost-aware GRPO** 
> 🏗️ built on top of [**verl**](https://github.com/volcengine/verl), whose training stack made the RL side tractable. 

## 🎆Configure Your Own Uno-Orchestra

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

**🧁 Stratified coverage sampling.** We construct the training pool by drawing a fixed quota from each source so that four orthogonal capability axes are each exercised by at least two datasets and no single axis dominates the mixture. This yields approximately 10k raw tasks. After bootstrapped curriculum filtering,


| Capability axis | What the router must learn | Datasets |
|---|---|---|
| **Atomic reasoning** | Forward the task to a single model | GSM8K |
| **Compositional reasoning** | Multi-step symbolic manipulation requiring chain-of-thought delegation | NuminaMath-CoT |
| **Knowledge retrieval** | Decompose into independent evidence-gathering subtasks | DROP, HotpotQA |
| **Knowledge composition** | Deep sequential decomposition with inter-subtask dependencies | MuSiQue |
| **Tool orchestration** | Select correct tool–model pairs and chain API calls | TACO, ToolACE |

### 🍭Data Selection Pipeline

We employ **bootstrapped curriculum filtering** on raw question sets from training data pools for supervised fine-tuning and reinforcement learning to ensure the training set consists entirely of router's capability gaps.

### 🍒Bootstrapped curriculum filtering

*Stage 1*: **Router probe**. We run the current router checkpoint on every task in the pool with real sub-model execution — the router decomposes the task, delegates sub-tasks to actual models, and produces a final answer. We evaluate each task via pass\@3 and check against the gold label. Tasks the router already solves correctly are discarded, as they carry no learning signal.

*Stage 2*: **Teacher trajectory** For each remaining task — where the router failed — we run a strong teacher orchestrator with the same model pool. If the teacher produces a correct trajectory, the task enters the SFT set as a demonstration for imitation learning. If the teacher also fails, the task enters the RL set, where the router must discover a working decomposition through its own exploration.

*Stage 3*: **Noise removal**: trajectories with infrastructure artifacts (API timeouts, incomplete responses) or dataset annotation errors (gold answers that are not valid API calls) are discarded, as they provide neither correct demonstrations nor meaningful reward signal.

This pipeline is self-adaptive: it can be re-applied after each training round to produce a curriculum of increasing difficulty, as the router's capability boundary shifts with training.


✅**Failure-Driven in context learning**
For each failed trajectory, we feed the full execution trace—including the Orchestrator's delegation decisions, sub-agent responses, and the final erroneous answer into GPT-4o, which diagnoses the root cause and assigns it to one of the following failure categories: (i) information loss which happens when the Orchestrator omits critical context when delegating subtasks; (ii) premature aggregation—intermediate results are returned without completing the final computation; (iii) format mismatch—the answer is semantically correct but does not conform to the expected output format; and (iv) delegation scope error—the task is under or over decomposed. Once failures are categorized, we generate a minimal, targeted constraint for each high-frequency category and inject it into the Orchestrator's instruction. Crucially, these patches are not instance-specific fixes tied to particular failing examples; rather, they clarify the Orchestrator's general understanding of the task protocol—such as what constitutes a complete answer or what information must be preserved during delegation. The resulting constraints are task-agnostic and transfer to unseen problems, since they address systematic gaps in how the Orchestrator interprets its role rather than surface-level errors on individual inputs.

📷This diagnostic-then-patch loop runs for 3 rounds. By the third round, the failure taxonomy reveals that all remaining errors stem from suboptimal routing decisions—such as dispatching a complex symbolic reasoning task to a lightweight model—rather than ambiguity in the Orchestrator's instructions. This indicates that prompt clarity has been saturated, and further gains require improving the Router's model selection policy.

## Dataset Description

Every SFT row comes from a real public HuggingFace dataset — the `question` and `gold_answer` are sampled verbatim from a `source` we record on the row. Every row then passes through the **same three-stage pipeline** (§ Data Selection Pipeline) — router probe → teacher trajectory → noise removal — to obtain the multi-turn trajectory that teaches the router how to handle that question.
For different tasks, we adopt different generation pipeline:

- **QA / reasoning / math** — the teacher (Claude Opus) derives the `<plan>/<route>/<obs>/<verify>/<final_answer>` trajectory directly from the question plus the dataset's own context / evidence field (Wikipedia passages for HotpotQA, search snippets for TriviaQA, the step-by-step solution for GSM8K, etc. — see § Distillation for the full evidence map). No external environment is invoked because these benchmarks don't have one.
- **Code (TACO) / tool use (ToolACE)** — the trajectory is produced through real runtime execution: routed `<route>` calls actually run code in the sandbox or actually fire tool calls against the schema, and the `<obs>` content is the executor's / API's real output, not a reconstruction.

In both cases the per-source verifier scores the teacher's `<final_answer>` against the real gold, so only gold-matching trajectories enter the SFT corpus.

### Sources (38 HuggingFace datasets, 9 categories)

| Category | Count | Share | `source` values |
|---|---:|---:|---|
| qa_multi_hop | 31,957 | 52.2% | hotpotqa_fullwiki, 2wikimultihopqa, musique_answerable, bamboogle, hotpotqa, musique |
| reasoning_commonsense | 8,465 | 13.8% | commonsenseqa, strategyqa, social_iqa, piqa, winogrande, logiqa2, arc_challenge, bbh_*, folio |
| qa_open_domain | 6,787 | 11.1% | nq_open, triviaqa_nocontext, webquestions, quality |
| knowledge_academic | 6,208 | 10.1% | mmlu_aux_stem, sciq, openbookqa, aqua_rat, theoremqa, legalbench |
| math | 4,597 | 7.5% | gsm8k_main, gsm8k, numinamath, hendrycks_math_{algebra, intermediate_algebra, number_theory} |
| code | 2,157 | 3.5% | codeforces_cots, codecontests, taco |
| tool_use | 705 | 1.2% | toolace |
| reading_comprehension | 289 | 0.5% | drop |
| other | 36 | 0.1% | misc rows lacking HF-side metadata |

### Two expansion passes on top of the base pipeline

1. **Rejection-sampled augmentation** (`scripts/data/augment_sft.py`): K=2 extra teacher rollouts at temperatures {0.5, 1.0} for every SFT question; K=3 at {0.3, 0.7, 1.0} for the harder RL-pool questions. Only trajectories that pass the per-source verifier survive — the gold label doubles as a consistency gate.

2. **Fallback distillation** (`scripts/data/rescue_rl_pool.py`): RL-pool questions — where the primary teacher (qwen3.5-plus) failed — are retried with a stronger cascade (gemini-2.5-pro → claude-sonnet-4-6 → gpt-5.4) under pass@3. Whichever cascade step resolves the question yields a trajectory that is promoted from the RL pool into the SFT corpus. This pass shrinks the RL pool from 4,549 to **2,976** tasks (−34.6%) by promoting 295 previously unsolvable questions; the largest gains are on tool orchestration, where gemini-2.5-pro's code generation resolves TACO tasks qwen3.5-plus could not.

Each row carries `teacher` (which model produced the trajectory) and `distillation_pass` (primary / augmentation / fallback) alongside `source`, so the provenance of every trajectory is fully traceable.

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

### Final SFT corpus

**61,201 multi-turn ShareGPT conversations** (system → human → assistant → observation → assistant → ...). Each row:

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
        │                - Loads (question, gold, evidence) from HF
        │                - Calls teacher model(s) to generate trajectory
        │                - Validates against schema (16 hard rules)
        │                - Outputs raw JSONL
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
        │                - Real worker-API calls via the xiaojingai proxy
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

Worker calls go through the **xiaojingai proxy**, which serves each of the 10 closed-vocabulary model names with authentic frontier pricing. Token usage is read from the API response for cost accounting; per-model `max_tokens` caps bound the episodic cost; a hard per-episode USD ceiling early-terminates any runaway rollout.

## 🥦 Error Taxonomy

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

### Worker Pool

10 models across 4 providers, 13 skills. Output cost ranges from \$0.40/M (gemini-2.5-flash-lite) to \$75/M (claude-opus-4-6). The router learns to balance accuracy vs cost — picking cheap models for easy subtasks and expensive models only when needed. Full definition in `configs/pools.yaml`.

## 🧪 Evaluation

Unified eval pipeline supporting any router on any benchmark:

```bash
python -m eval_pipeline.run --router ROUTER --bench BENCH --api_key KEY

# Routers:    router-r1, uno-sft, uno-rl, direct,
#             random, oracle-cheapest, router+claude, oracle-codex
# Benchmarks: swebench (500 instances), terminalbench (89 tasks),
#             plus the 7-source held-out RL pool
```

Verification uses official methods:
- **SWE-bench**: `swebench.harness.run_evaluation` (Docker apply + test suite)
- **Terminal-Bench**: Harbor Docker (container per task, `test.sh` verification)

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

### Current Progress

| Baseline | SWE-bench (500) | Terminal-Bench (89) |
|----------|:---:|:---:|
| Router-R1 | 500 gen, 500 verified | 500 gen, 30/89 verified |
| Uno-SFT | 500 gen | 89 gen, 17/89 verified |
| Direct(Qwen2.5-7B) | 500 gen | 89 gen, 19/89 verified |
| Direct(GPT-5.4) | 500 gen, 500 verified | 89 gen, 25/89 verified |
| Oracle-Codex | 500 gen, 500 verified | 89 gen, 16/89 verified |
| router+claude | 500 gen, 500 verified | 89 gen, 27/89 verified |
| Oracle-Cheapest | 500 gen, 500 verified | 89 gen, 29/89 verified |
| Random | 500 gen, 500 verified | 89 gen, 29/89 verified |

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
    rl/                        # GiGPO / GRPO RL training scripts
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
