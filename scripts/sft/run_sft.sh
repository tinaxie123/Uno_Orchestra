set -euo pipefail
LF_DIR="${LF_DIR:-}"
VENV_PY="${VENV_PY:-}"
CONFIG_PATH="${CONFIG_PATH:-}"
MASTER_PORT="${MASTER_PORT:-41467}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
LOG="${LOG:-logs/train_sft.log}"
NPROC=$(echo "$CUDA_VISIBLE_DEVICES" | awk -F, '{print NF}')

if [[ -z "$LF_DIR" || -z "$VENV_PY" || -z "$CONFIG_PATH" ]]; then
    cat <<'EOF'
Usage:
  LF_DIR=/path/to/LlamaFactory \
  VENV_PY=/path/to/venv/bin \
  CONFIG_PATH=/path/to/llamafactory_sft.yaml \
  CUDA_VISIBLE_DEVICES=0,1,2,3 \
  bash scripts/sft/run_sft.sh

This launcher is intentionally configuration-driven. Keep project-specific SFT
recipes outside the eval framework or add them under configs/sft/.
EOF
    exit 2
fi

echo "[run_sft] LF_DIR=$LF_DIR"
echo "[run_sft] GPUs=$CUDA_VISIBLE_DEVICES (nproc=$NPROC)"
echo "[run_sft] config=$CONFIG_PATH"
echo "[run_sft] log=$LOG"

cd "$LF_DIR"

mkdir -p "$(dirname "$LOG")" "${TMPDIR:-/tmp/uno_sft}"
CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
PYTHONPATH="$LF_DIR/src" \
TMPDIR="${TMPDIR:-/tmp/uno_sft}" \
HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-/tmp/uno_sft/hf_datasets}" \
WANDB_PROJECT="${WANDB_PROJECT:-uno-sft}" \
nohup "$VENV_PY/torchrun" \
    --nnodes 1 --node_rank 0 --nproc_per_node "$NPROC" \
    --master_addr 127.0.0.1 --master_port "$MASTER_PORT" \
    "$LF_DIR/src/llamafactory/launcher.py" \
    "$CONFIG_PATH" \
    > "$LOG" 2>&1 &

PID=$!
echo "[run_sft] launched pid=$PID; tail -f $LOG"
