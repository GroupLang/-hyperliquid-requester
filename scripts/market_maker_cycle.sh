#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LOG_DIR=${LOG_DIR:-"${ROOT_DIR}/logs"}
mkdir -p "${LOG_DIR}"
STAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/marketmaker_${STAMP}.log"

export PYTHONPATH="${ROOT_DIR}/src:${PYTHONPATH:-}"

{
  echo "[${STAMP}] Starting Hyperliquid requester cycle"
  python -m hyperliquid_requester.market_maker --execute "$@"
  EXIT_CODE=$?
  echo "[${STAMP}] Cycle finished with exit code ${EXIT_CODE}"
  exit ${EXIT_CODE}
} | tee -a "${LOG_FILE}"
