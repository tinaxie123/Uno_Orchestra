# Data & Training Pipeline

End-to-end pipeline for the selective-delegation router: from raw HuggingFace datasets to a trained Router model.

## Pipeline Overview

```
HuggingFace Datasets (31 sources, 10 domains)
        |
        v
[1] generate_trajectories.py          Teacher distillation (API calls)
        |                - Loads (question, gold, evidence) from HF
        |                - Calls teacher model to generate trajectory
        |                - Validates against schema
        |                - Outputs: phase_c_final.jsonl
        v
[2] build_dataset.py  Build training set
        |                - Re-validates all samples
        |                - Applies filter rules
        |                - Outputs: train_final.parquet
        v
[3] SFT Training        Fine-tune base model (8x H100)
        |                - ChatML format, mask non-assistant turns
        |                - 3 epochs, lr=2e-5, DeepSpeed ZeRO-2
        v
    Router Model         Trained router for evaluation
```

---

## Step 1: Distillation (`scripts/generate_trajectories.py`)

Generates SFT trajectories by calling a teacher model (claude-sonnet-4-6, claude-opus-4-6, or qwen-max) via OpenAI-compatible API.

### How it works

1. Loads `config/sft_recipe.yaml` — defines 31 datasets across 10 domains with per-dataset sample counts
2. For each dataset, streams (question, gold_answer) pairs from HuggingFace
3. Extracts real evidence from dataset fields when available (e.g. Wikipedia context for HotpotQA, search results for TriviaQA, step-by-step solutions for GSM8K)
4. Injects evidence into the teacher prompt so the teacher writes obs based on real data
5. Teacher generates a full trajectory: `<plan>` → `<route>` → `<obs>` → `<verify>` → `<final_answer>`
6. `validate_schema.py` validates against all 16 rules in schema
7. Valid samples appended to output JSONL; invalid go to `_failed.jsonl`
8. Resume: reads existing output file IDs and skips already-generated samples

### Usage

```bash
# Environment variables
export XIAOJING_API_KEY="sk-..."        # OpenAI-compatible API key
export HF_TOKEN="hf_..."               # HuggingFace token (faster downloads)
export HF_ENDPOINT="https://hf-mirror.com"  # Optional: Chinese mirror

# Full distillation (all 31 datasets)
python3 scripts/generate_trajectories.py --full --concurrency 200 --out-name phase_c_final

# Single dataset
python3 scripts/generate_trajectories.py --only hotpotqa_fullwiki --n 100

# With different API endpoint / model
python3 scripts/generate_trajectories.py --full --concurrency 1000 \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --api-key sk-... \
  --recipe config/sft_recipe_qwen.yaml  # recipe with distill_model: qwen-max
```

### Evidence extraction

The distiller automatically extracts real evidence from dataset fields:

| Source | Evidence field | Example |
|--------|---------------|---------|
| hotpotqa_fullwiki | context (Wikipedia passages) | `[Title] sentence1 sentence2...` |
| 2wikimultihopqa | evidences | Supporting fact sentences |
| musique_answerable | paragraphs | Paragraph texts |
| strategyqa | evidence | Evidence list |
| triviaqa (rc split) | search_results.search_context | Web search snippets |
| gsm8k | answer | Step-by-step solution |
| hendrycks_math | solution | LaTeX solution |
| codeforces_cots | editorial | Problem editorial |
| sciq | support | Supporting passage |
| logiqa2, folio, bbh | text/premises/input | Problem context |
| quality | article | Full article (truncated 3k chars) |

Datasets without evidence (nq_open, webquestions, arc, mmlu, commonsenseqa, piqa, social_iqa, winogrande) use teacher's own knowledge — obs quality is slightly lower but acceptable.

### Output format

Each line in the JSONL is a complete trajectory:

```json
{
  "id": "hotpotqa_fullwiki_93331229",
  "source": "hotpotqa_fullwiki",
  "domain": "multihop_qa",
  "behavior": "oneshot",
  "teacher": "claude-sonnet-4-6",
  "messages": [
    {"role": "system", "content": "You are generating ONE training trajectory..."},
    {"role": "user", "content": "Question: ... \nCorrect answer: ...\nREAL EVIDENCE: ..."},
    {"role": "assistant", "content": "<plan round=\"1\">...<route ...>...</route>"},
    {"role": "tool", "content": "<obs subtask=\"1\">...</obs>"},
    {"role": "assistant", "content": "<verify ...>...<final_answer>...</final_answer>"}
  ],
  "gold": "correct answer",
  "valid": true,
  "stats": { "is_lazy": false, "n_plan_rounds": 1, "n_routes": 2, ... }
}
```

### Trajectory behaviors (4 types)

- **lazy** (15.6%): Direct answer without decomposition. Teaches model when NOT to delegate.
- **oneshot** (49.5%): Single-round plan→route→obs→verify→answer. Clean parallel decomposition.
- **continuation** (30.4%): Multi-round. Round 1 explores, round 2+ plans based on round 1 obs.
- **decomp_repair** (4.4%): Verify detects issues, triggers re-plan with targeted repair.

---

## Step 2: Build Training Set (`scripts/build_dataset.py`)

Converts raw JSONL to a validated parquet file for training.

```bash
python3 scripts/build_dataset.py \
  --inputs data/sft/phase_c_final.jsonl \
  --snapshot phase_c_final
```

Applies:
- Schema re-validation
- Filter rules (max attempts, max tokens, max routes)
- Behavior classification
- Outputs `train_final.parquet` + `train_final_stats.json`

### Quality audit (`scripts/audit_quality.py`)

```bash
python3 scripts/audit_quality.py data/sft/phase_c_final.jsonl --verbose
```

Checks: schema validation, message structure, obs quality, gold match, duplicates, domain coverage.

---

## Step 3: SFT Training

### Data location

Server: `/home/xieht/data/sft/train_final.parquet` (58,457 samples, 166MB)

### Training script

```python
# train_sft.py — run with: torchrun --nproc_per_node=8 train_sft.py
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM
from trl import SFTTrainer, SFTConfig
import json

dataset = load_dataset("parquet", data_files="/home/xieht/data/sft/train_final.parquet", split="train")

def parse_messages(example):
    msgs = example["messages"]
    if isinstance(msgs, str):
        msgs = json.loads(msgs)
    example["messages"] = msgs
    return example

dataset = dataset.map(parse_messages)
dataset = dataset.train_test_split(test_size=0.02, seed=42)

model_name = "Qwen/Qwen2.5-7B-Instruct"
tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="bfloat16", trust_remote_code=True)

training_args = SFTConfig(
    output_dir="/home/xieht/data/sft/checkpoints",
    num_train_epochs=3,
    per_device_train_batch_size=2,
    gradient_accumulation_steps=8,   # effective batch = 2*8*8GPUs = 128
    learning_rate=2e-5,
    lr_scheduler_type="cosine",
    warmup_ratio=0.05,
    weight_decay=0.01,
    max_seq_length=4096,
    bf16=True,
    logging_steps=10,
    save_steps=500,
    eval_strategy="steps",
    eval_steps=500,
    save_total_limit=3,
    deepspeed="ds_config_zero2.json",
    dataloader_num_workers=4,
    remove_unused_columns=False,
)

trainer = SFTTrainer(
    model=model, args=training_args,
    train_dataset=dataset["train"], eval_dataset=dataset["test"],
    processing_class=tokenizer,
)
trainer.train()
trainer.save_model("/home/xieht/data/sft/router_final")
```

### DeepSpeed config (`ds_config_zero2.json`)

```json
{
    "bf16": {"enabled": true},
    "zero_optimization": {
        "stage": 2,
        "allgather_partitions": true,
        "allgather_bucket_size": 2e8,
        "reduce_scatter": true,
        "reduce_bucket_size": 2e8
    },
    "gradient_accumulation_steps": "auto",
    "gradient_clipping": 1.0,
    "train_batch_size": "auto",
    "train_micro_batch_size_per_gpu": "auto"
}
```

### Launch

```bash
cd /home/xieht/data/sft
torchrun --nproc_per_node=8 train_sft.py
```

Expected: ~2-3 hours on 8x H100 for 3 epochs.

---

## Final Dataset Stats

| Metric | Value |
|--------|-------|
| Total samples | 58,457 |
| Domains | 9/10 (tool_agent skipped due to HF download issues) |
| Schema pass | 100% |
| Behavior match | 96.6% |
| Teachers | claude-sonnet (69%), claude-opus (18%), qwen-max (13%) |
| Skills used | 13 (direct_answer, web_search, reason, ...) |
| Total cost | $82.95 |

### Domain distribution

| Domain | Samples | Key datasets |
|--------|---------|-------------|
| multihop_qa | 33,016 | hotpotqa, 2wiki, musique, strategyqa |
| stem | 6,617 | sciq, arc, openbookqa, mmlu |
| single_hop_lazy | 5,915 | nq_open, triviaqa, webquestions |
| math | 5,008 | gsm8k, hendrycks_math, aqua_rat |
| commonsense_social | 4,222 | commonsenseqa, piqa, social_iqa, winogrande |
| formal_logic | 1,749 | logiqa2, folio, bbh |
| code | 1,148 | codeforces, codecontests |
| long_context | 872 | quality |
| domain_knowledge | 5 | legalbench |

### Key design decisions

1. **Real evidence over synthetic obs**: Wherever datasets provide context/evidence fields, inject them into the teacher prompt so obs contain factual information, not hallucinations.
2. **Multiple teacher models**: claude-sonnet for easy/medium, claude-opus for hard, qwen-max for supplementary generation. Different teachers provide trajectory diversity.
3. **Train/test split safety**: All training data from train splits. BBH (test-only, 400 samples) may overlap with eval if BBH is used for evaluation.
4. **Behavioral diversity**: 4 trajectory types (lazy/oneshot/continuation/decomp_repair) teach the router different delegation strategies.
