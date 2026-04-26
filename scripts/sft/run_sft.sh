set -euo pipefail
LF_DIR="${LF_DIR:-/data/xieht/LlamaFactory}"
VENV_PY="${VENV_PY:-/data/xieht/sft/venv_sft/bin}"
CONFIG_REL="examples/train_full/router_sft_qwen25_7b.yaml"
MASTER_PORT="${MASTER_PORT:-41467}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-4,5,6,7}"
LOG="${LOG:-/data/xieht/sft/train_sft.log}"
NPROC=$(echo "$CUDA_VISIBLE_DEVICES" | awk -F, '{print NF}')

echo "[run_sft] LF_DIR=$LF_DIR"
echo "[run_sft] GPUs=$CUDA_VISIBLE_DEVICES (nproc=$NPROC)"
echo "[run_sft] config=$LF_DIR/$CONFIG_REL"
echo "[run_sft] log=$LOG"

cd "$LF_DIR"

mkdir -p /data/xieht/tmp_sft
CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
PYTHONPATH="$LF_DIR/src" \
TMPDIR=/data/xieht/tmp_sft \
HF_DATASETS_CACHE=/data/xieht/tmp_sft/hf_datasets \
WANDB_PROJECT=uno-sft \
nohup "$VENV_PY/torchrun" \
    --nnodes 1 --node_rank 0 --nproc_per_node "$NPROC" \
    --master_addr 127.0.0.1 --master_port "$MASTER_PORT" \
    "$LF_DIR/src/llamafactory/launcher.py" \
    "$CONFIG_REL" \
    > "$LOG" 2>&1 &

PID=$!
echo "[run_sft] launched pid=$PID; tail -f $LOG"
