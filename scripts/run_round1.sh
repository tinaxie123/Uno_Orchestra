#!/bin/bash
# Round 1: Full bootstrapped curriculum filtering (~10k tasks)
# Pipeline: Router probe (pass@3) → Teacher trajectory → Overlong filter → SFT/RL split
# All API calls go through xiaojingai. No Qwen in sub-agent pool.
set -euo pipefail

PYTHON=/home/xieht/.conda/envs/marl/bin/python
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Planner + Router: local Qwen 2.5 7B
PLANNER_MODEL="Qwen/Qwen2.5-7B-Instruct"
PLANNER_API_BASE="http://localhost:8236/v1"
ROUTER_MODEL="Qwen/Qwen2.5-7B-Instruct"
ROUTER_API_BASE="http://localhost:8236/v1"

# Teacher + Sub-agent: all xiaojingai
API_BASE="https://open.xiaojingai.com/v1/"
API_KEY="sk-wFh8h2dhytX3J7ywOZld4IVWoEoBr8hZ8DonD60UYHDZSrYT"
TEACHER_MODEL="qwen3.5-plus"

OUT_DIR="${REPO_ROOT}/data/sft/round1"
CONCURRENCY=32
SEED=42

mkdir -p "${OUT_DIR}"

echo "=== Round 1: Bootstrapped Curriculum Filtering ==="
echo "Planner/Router: ${PLANNER_MODEL} @ ${PLANNER_API_BASE}"
echo "Teacher:        ${TEACHER_MODEL} @ ${API_BASE}"
echo "Sub-agent:      ${API_BASE} (no Qwen in pool)"
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
    --teacher-api-base "${API_BASE}" \
    --teacher-api-key "${API_KEY}" \
    --sub-model-api-base "${API_BASE}" \
    --sub-model-api-key "${API_KEY}" \
    --out-dir "${OUT_DIR}" \
    --concurrency "${CONCURRENCY}" \
    --seed "${SEED}"
