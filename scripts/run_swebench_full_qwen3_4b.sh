#!/bin/bash
set -euo pipefail

PYTHON=/home/xieht/.conda/envs/marl/bin/python
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
VLLM_ENV=${VLLM_ENV:-rlanything}

MODEL_PATH="/data/MODEL/Qwen3-4B"
SERVED_MODEL="Qwen/Qwen3-4B"
LOCAL_BASE="http://127.0.0.1:8235/v1"
VLLM_PORT=8235
TP_SIZE=2
GPU_IDS="0,1"
API_BASE="${API_BASE:-https://open.xiaojingai.com/v1/}"
EVAL_API_KEY="${EVAL_API_KEY:-}"
OUT_DIR="${REPO_ROOT}/data/eval/agentic_baseline/swebench_full_qwen3_4b"
GEN_WORKERS=${GEN_WORKERS:-8}
VERIFY_WORKERS=${VERIFY_WORKERS:-4}
MAX_TASKS="${MAX_TASKS:-}"
HF_HOME="${HF_HOME:-/data/xieht/.cache/huggingface}"
HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-${HF_HOME}/datasets}"

if [[ -z "${EVAL_API_KEY}" ]]; then
  echo "ERROR: EVAL_API_KEY is empty."
  echo "Usage: EVAL_API_KEY=sk-xxx bash scripts/run_swebench_full_qwen3_4b.sh"
  exit 1
fi

mkdir -p "${OUT_DIR}"
mkdir -p "${HF_HOME}" "${HF_DATASETS_CACHE}"

echo "=== SWE-bench full (Qwen3-4B local router) ==="
echo "Model:          ${SERVED_MODEL} (${MODEL_PATH})"
echo "Local base:     ${LOCAL_BASE}"
echo "GPU:            ${GPU_IDS} (tp=${TP_SIZE})"
echo "Output dir:     ${OUT_DIR}"
echo "gen/verify:     ${GEN_WORKERS}/${VERIFY_WORKERS}"
echo "HF cache:       ${HF_DATASETS_CACHE}"
echo ""

# Stop any existing vLLM bound to this port
pkill -f "vllm.entrypoints.openai.api_server.*--port ${VLLM_PORT}" || true
sleep 1

echo "[1/2] Starting vLLM on ${LOCAL_BASE} ..."
nohup bash -lc "CUDA_VISIBLE_DEVICES=${GPU_IDS} conda run -n ${VLLM_ENV} python3 -m vllm.entrypoints.openai.api_server \
  --model ${MODEL_PATH} \
  --served-model-name ${SERVED_MODEL} \
  --port ${VLLM_PORT} --host 127.0.0.1 \
  --tensor-parallel-size ${TP_SIZE} \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice --tool-call-parser hermes" \
  > "${OUT_DIR}/vllm_qwen3_4b.log" 2>&1 &

for _ in $(seq 1 30); do
  if ss -ltn | grep -q ":${VLLM_PORT} "; then
    break
  fi
  sleep 1
done
if ! ss -ltn | grep -q ":${VLLM_PORT} "; then
  echo "ERROR: vLLM did not start on port ${VLLM_PORT}. Check ${OUT_DIR}/vllm_qwen3_4b.log"
  exit 1
fi

pkill -f "python -m eval_pipeline .*--bench swebench .*swebench_full" || true
sleep 1

echo "[2/2] Starting SWE-bench full eval ..."
CMD=(
  "${PYTHON}" -m eval_pipeline
  --router local
  --bench swebench
  --api_key "${EVAL_API_KEY}"
  --api_base "${API_BASE}"
  --local_base "${LOCAL_BASE}"
  --output_dir "${OUT_DIR}"
  --gen_workers "${GEN_WORKERS}"
  --verify_workers "${VERIFY_WORKERS}"
  --interactive
)
if [[ -n "${MAX_TASKS}" ]]; then
  CMD+=(--max_tasks "${MAX_TASKS}")
fi

nohup env HF_HOME="${HF_HOME}" HF_DATASETS_CACHE="${HF_DATASETS_CACHE}" "${CMD[@]}" > "${OUT_DIR}/run.log" 2>&1 &

echo "Started."
echo "Logs:"
echo "  tail -f ${OUT_DIR}/run.log"
echo "  tail -f ${OUT_DIR}/vllm_qwen3_4b.log"
echo "Progress files:"
echo "  ${OUT_DIR}/predictions.jsonl"
echo "  ${OUT_DIR}/verification.jsonl"
