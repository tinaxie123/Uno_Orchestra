#!/bin/bash
# Round 1 (Qwen3-4B): Same pipeline as round1 but with Qwen3-4B-Instruct as planner/router
# GPU 5: vLLM Qwen3-4B-Instruct @ port 8235
# Teacher: qwen3.5-plus (DashScope, same as round1)
# Sub-agent: xiaojingai API
set -euo pipefail

PYTHON=/home/xieht/.conda/envs/marl/bin/python
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Planner + Router: local Qwen3-4B-Instruct (GPU 5, port 8235)
PLANNER_MODEL="Qwen/Qwen3-4B-Instruct"
PLANNER_API_BASE="http://localhost:8235/v1"
ROUTER_MODEL="Qwen/Qwen3-4B-Instruct"
ROUTER_API_BASE="http://localhost:8235/v1"

# Teacher: DashScope qwen3.5-plus (same as round1)
TEACHER_MODEL="qwen3.5-plus"
TEACHER_API_BASE="https://dashscope.aliyuncs.com/compatible-mode/v1"
TEACHER_API_KEY="sk-70793c3ec75a40ca90b7076de9927260"

# Sub-agent: xiaojingai
SUB_API_BASE="https://open.xiaojingai.com/v1/"
SUB_API_KEY="sk-7Yv0jGTYpFErrvpnVIlz3cGOtxIuwucfMT5fSxYalwVWprL0"

OUT_DIR="${REPO_ROOT}/data/sft/round1_q3"
CONCURRENCY=32
SEED=42

mkdir -p "${OUT_DIR}"

echo "=== Round 1 (Qwen3-4B): Bootstrapped Curriculum Filtering ==="
echo "Planner/Router: ${PLANNER_MODEL} @ ${PLANNER_API_BASE}"
echo "Teacher:        ${TEACHER_MODEL} @ ${TEACHER_API_BASE}"
echo "Sub-agent:      ${SUB_API_BASE}"
echo "Concurrency:    ${CONCURRENCY}"
echo "Output:         ${OUT_DIR}"
echo ""

$PYTHON "${REPO_ROOT}/scripts/data/generate_trajectories.py" \
    --planner-model "${PLANNER_MODEL}" \
    --planner-api-base "${PLANNER_API_BASE}" \
    --planner-api-key "none" \
    --router-model "${ROUTER_MODEL}" \
    --router-api-base "${ROUTER_API_BASE}" \
    --router-api-key "none" \
    --teacher-model "${TEACHER_MODEL}" \
    --teacher-api-base "${TEACHER_API_BASE}" \
    --teacher-api-key "${TEACHER_API_KEY}" \
    --sub-model-api-base "${SUB_API_BASE}" \
    --sub-model-api-key "${SUB_API_KEY}" \
    --out-dir "${OUT_DIR}" \
    --concurrency "${CONCURRENCY}" \
    --seed "${SEED}"
