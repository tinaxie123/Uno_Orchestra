#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

exec bash "$PROJECT_DIR/scripts/rl/run_grpo_uno.sh" \
  algorithm.adv_estimator=grpo \
  "$@"
