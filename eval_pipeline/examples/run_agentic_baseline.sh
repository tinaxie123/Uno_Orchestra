#!/usr/bin/env bash
# ─── Pre-SFT agentic baseline: router(Qwen2.5-7B) → API sub-agents ───
# GPU 6: SWE-bench (port 8236)
# GPU 7: Terminal-Bench (port 8237)
#
# Usage:
#   bash eval_pipeline/examples/run_agentic_baseline.sh          # launch both
#   bash eval_pipeline/examples/run_agentic_baseline.sh --status  # check progress
set -euo pipefail

API_KEY="sk-wFh8h2dhytX3J7ywOZld4IVWoEoBr8hZ8DonD60UYHDZSrYT"
API_BASE="https://open.xiaojingai.com/v1/"
MODEL="/data/MODEL/Qwen2.5-7B-Instruct"
MODEL_NAME="Qwen/Qwen2.5-7B-Instruct"
OUT="/data/xieht/multiagentRL/data/eval/agentic_baseline"

if [[ "${1:-}" == "--status" ]]; then
    echo "=== Processes ==="
    ps aux | grep -E "eval_pipeline|vllm.*823[67]" | grep -v grep | awk '{print $2, $NF}' || echo "(none)"
    echo "=== SWE-bench ==="
    [[ -f "$OUT/swebench/summary.json" ]] && cat "$OUT/swebench/summary.json" || echo "(running or not started)"
    echo "=== Terminal-Bench ==="
    [[ -f "$OUT/terminalbench/summary.json" ]] && cat "$OUT/terminalbench/summary.json" || echo "(running or not started)"
    exit 0
fi

mkdir -p "$OUT"

# Wait for vLLM ready
wait_vllm() {
    local port=$1 timeout=120
    echo -n "Waiting vLLM :$port"
    for i in $(seq 1 $timeout); do
        curl -s "http://localhost:$port/v1/models" >/dev/null 2>&1 && echo " ready" && return 0
        echo -n "."; sleep 1
    done
    echo " TIMEOUT"; return 1
}

wait_vllm 8236 || exit 1
wait_vllm 8237 || exit 1

echo "=== Launching SWE-bench (GPU 6, port 8236, 100 tasks) ==="
nohup conda run -n marl python -m eval_pipeline \
    --router local --bench swebench \
    --api_key "$API_KEY" --api_base "$API_BASE" \
    --local_base http://localhost:8236/v1 \
    --output_dir "$OUT/swebench" \
    --max_tasks 100 --gen_workers 4 --verify_workers 4 \
    --interactive \
    > "$OUT/swebench.log" 2>&1 &
echo "PID: $!"

echo "=== Launching Terminal-Bench (GPU 7, port 8237, 70 tasks) ==="
nohup conda run -n marl python -m eval_pipeline \
    --router local --bench terminalbench \
    --api_key "$API_KEY" --api_base "$API_BASE" \
    --local_base http://localhost:8237/v1 \
    --output_dir "$OUT/terminalbench" \
    --max_tasks 70 --gen_workers 4 --verify_workers 4 \
    --interactive \
    > "$OUT/terminalbench.log" 2>&1 &
echo "PID: $!"

echo "=== Both launched. Check: bash $0 --status ==="
