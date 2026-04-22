#!/bin/bash
# SFT warm-start training for SkillRouter
#
# Prerequisites:
#   1. Install LlamaFactory: pip install -e ".[torch,metrics]"
#   2. Install deepspeed: pip install deepspeed
#   3. Prepare data: python scripts/sft/prepare_data.py \
#        --input data/sft/train_final.parquet \
#        --output $LLAMA_FACTORY_DIR/data/router_sft_sharegpt.json
#   4. Register dataset in $LLAMA_FACTORY_DIR/data/dataset_info.json:
#        "router_sft_v2": {
#          "file_name": "router_sft_sharegpt.json",
#          "formatting": "sharegpt",
#          "columns": {"messages": "conversations"}
#        }
#
# Usage:
#   CUDA_VISIBLE_DEVICES=4,5,6,7 bash scripts/sft/run_sft.sh

set -e

LLAMA_FACTORY_DIR="${LLAMA_FACTORY_DIR:-/home/xieht/data/LlamaFactory}"
CONFIG_REL="${CONFIG_REL:-configs/sft/sft_qwen25_7b.yaml}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
CONFIG_ABS="$REPO_DIR/$CONFIG_REL"

cd "$LLAMA_FACTORY_DIR"

FORCE_TORCHRUN=1 \
WANDB_PROJECT=router-sft \
WANDB_ENTITY=multiagent_router \
llamafactory-cli train "$CONFIG_ABS"
