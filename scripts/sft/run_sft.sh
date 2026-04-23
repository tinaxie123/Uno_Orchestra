#!/bin/bash
# SFT launcher for SkillRouter (Qwen2.5-7B-Instruct, full FT, ZeRO-2, 2 epochs).
#
# Trains the single-policy hierarchical router described in the README
# (§🍏 Hierarchical SFT): Stage 1 <plan> + Stage 2 <route> are emitted in the same
# assistant turn, observations live in a dedicated `observation` role masked by
# LlamaFactory via `observation_tag: observation`.
#
# Usage:
#     bash scripts/sft/run_sft.sh                   # default GPUs 4,5,6,7
#     CUDA_VISIBLE_DEVICES=0,1,2,3 bash scripts/sft/run_sft.sh
#
# Prerequisites:
#   - LlamaFactory installed in /data/xieht/sft/venv_sft (with deepspeed + wandb)
#   - Dataset `router_sft` registered in /data/xieht/LlamaFactory/data/dataset_info.json
#     (pointing at router_sft.json, ShareGPT formatting, observation_tag: observation)
#   - Qwen2.5-7B-Instruct weights at /home/xieht/data/models/Qwen/Qwen2.5-7B-Instruct-real
#
# Notes / pitfalls we fixed along the way:
#   - Must use the venv_sft torchrun + set PYTHONPATH explicitly; otherwise LF's
#     launcher cannot import llamafactory.*  (previously surfaced as ModuleNotFoundError).
#   - cutoff_len=16384 to cover the long tail of the 61k-row corpus (p99 = 5.7k,
#     max = 13.1k). Earlier runs with cutoff_len=3500 silently truncated the
#     question because the system prompt alone is ~3.5k tokens.
#   - gradient_checkpointing=true to keep activations in check at 16k seq len.
#   - packing=true folds short samples together so cutoff_len ~= pack size,
#     not per-sample length.

set -euo pipefail

# ---- config ---------------------------------------------------------------
LF_DIR="${LF_DIR:-/data/xieht/LlamaFactory}"
VENV_PY="${VENV_PY:-/data/xieht/sft/venv_sft/bin}"
CONFIG_REL="examples/train_full/router_sft_qwen25_7b.yaml"
MASTER_PORT="${MASTER_PORT:-41467}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-4,5,6,7}"
LOG="${LOG:-/data/xieht/sft/train_sft.log}"
# ---------------------------------------------------------------------------

NPROC=$(echo "$CUDA_VISIBLE_DEVICES" | awk -F, '{print NF}')

echo "[run_sft] LF_DIR=$LF_DIR"
echo "[run_sft] GPUs=$CUDA_VISIBLE_DEVICES (nproc=$NPROC)"
echo "[run_sft] config=$LF_DIR/$CONFIG_REL"
echo "[run_sft] log=$LOG"

cd "$LF_DIR"

CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
PYTHONPATH="$LF_DIR/src" \
WANDB_PROJECT=skillrouter-sft \
nohup "$VENV_PY/torchrun" \
    --nnodes 1 --node_rank 0 --nproc_per_node "$NPROC" \
    --master_addr 127.0.0.1 --master_port "$MASTER_PORT" \
    "$LF_DIR/src/llamafactory/launcher.py" \
    "$CONFIG_REL" \
    > "$LOG" 2>&1 &

PID=$!
echo "[run_sft] launched pid=$PID; tail -f $LOG"
