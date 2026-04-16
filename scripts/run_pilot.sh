#!/bin/bash
# Pilot run: 20 tasks to verify end-to-end pipeline
# Usage: bash scripts/run_pilot.sh

set -euo pipefail

# --- Config ---
PYTHON=/home/xieht/.conda/envs/marl/bin/python
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Router: local Qwen 2.5 7B via vLLM
ROUTER_MODEL="Qwen/Qwen2.5-7B-Instruct"
ROUTER_API_BASE="http://localhost:8234/v1"
ROUTER_API_KEY="none"

# Teacher: Claude Sonnet via xiaojingai
TEACHER_MODEL="claude-sonnet-4-6"
TEACHER_API_BASE="https://open.xiaojingai.com/v1/"
TEACHER_API_KEY="${TEACHER_API_KEY:?Set TEACHER_API_KEY env var}"

# Sub-models: xiaojingai gateway (routes to gemini, qwen, gpt, claude, kimi)
SUB_MODEL_API_BASE="https://open.xiaojingai.com/v1/"
SUB_MODEL_API_KEY="${SUB_MODEL_API_KEY:?Set SUB_MODEL_API_KEY env var}"

# Output
OUT_DIR="${REPO_ROOT}/data/sft/pilot"
MAX_TASKS=20
SEED=42

# --- Pre-checks ---
echo "=== Pre-checks ==="
echo -n "Router API: "
curl -s "${ROUTER_API_BASE}/models" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null || { echo "FAILED - is vLLM running on port 8234?"; exit 1; }

echo -n "Teacher API: "
curl -s -H "Authorization: Bearer ${TEACHER_API_KEY}" "${TEACHER_API_BASE}models" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d[\"data\"])} models available')" 2>/dev/null || { echo "FAILED"; exit 1; }

echo ""
echo "=== Running pilot (${MAX_TASKS} tasks) ==="
echo "Router:  ${ROUTER_MODEL} @ ${ROUTER_API_BASE}"
echo "Teacher: ${TEACHER_MODEL} @ ${TEACHER_API_BASE}"
echo "Sub-models: ${SUB_MODEL_API_BASE}"
echo "Output:  ${OUT_DIR}"
echo ""

$PYTHON "${REPO_ROOT}/scripts/data/generate_trajectories.py" \
    --router-model "${ROUTER_MODEL}" \
    --router-api-base "${ROUTER_API_BASE}" \
    --router-api-key "${ROUTER_API_KEY}" \
    --teacher-model "${TEACHER_MODEL}" \
    --teacher-api-base "${TEACHER_API_BASE}" \
    --teacher-api-key "${TEACHER_API_KEY}" \
    --sub-model-api-base "${SUB_MODEL_API_BASE}" \
    --sub-model-api-key "${SUB_MODEL_API_KEY}" \
    --out-dir "${OUT_DIR}" \
    --max-tasks "${MAX_TASKS}" \
    --seed "${SEED}"

echo ""
echo "=== Done ==="
echo "SFT data: ${OUT_DIR}/sft.jsonl"
echo "RL data:  ${OUT_DIR}/rl.jsonl"
echo "Report:   ${OUT_DIR}/curriculum_report.json"
wc -l "${OUT_DIR}"/*.jsonl 2>/dev/null || true
