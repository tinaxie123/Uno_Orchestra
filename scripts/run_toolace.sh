#!/bin/bash
# Re-run toolace with tool definitions injected into questions
set -euo pipefail

PYTHON="${PYTHON:-python}"
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Router: local Qwen 2.5 7B
ROUTER_MODEL="Qwen/Qwen2.5-7B-Instruct"
ROUTER_API_BASE="http://localhost:8236/v1"

# Teacher + Sub-agent
API_BASE="${REMOTE_API_BASE:-https://open.xiaojingai.com/v1/}"
API_KEY="${REMOTE_API_KEY:?set REMOTE_API_KEY in your shell env}"
TEACHER_MODEL="qwen3.5-plus"

OUT_DIR="${REPO_ROOT}/data/sft/round1_toolace"
CONCURRENCY=32

mkdir -p "${OUT_DIR}"

echo "=== Toolace Re-run (with tool definitions in prompt) ==="
echo "Router:   ${ROUTER_MODEL} @ ${ROUTER_API_BASE}"
echo "Teacher:  ${TEACHER_MODEL} @ ${API_BASE}"
echo "Output:   ${OUT_DIR}"
echo ""

$PYTHON "${REPO_ROOT}/scripts/data/generate_trajectories.py" \
    --recipe "${REPO_ROOT}/configs/sft/data/toolace_only.yaml" \
    --planner-model "${ROUTER_MODEL}" \
    --planner-api-base "${ROUTER_API_BASE}" \
    --planner-api-key "none" \
    --router-model "${ROUTER_MODEL}" \
    --router-api-base "${ROUTER_API_BASE}" \
    --router-api-key "none" \
    --teacher-model "${TEACHER_MODEL}" \
    --teacher-api-base "${API_BASE}" \
    --teacher-api-key "${API_KEY}" \
    --sub-model-api-base "${API_BASE}" \
    --sub-model-api-key "${API_KEY}" \
    --out-dir "${OUT_DIR}" \
    --concurrency "${CONCURRENCY}" \
    --seed 42
