#!/bin/bash
# BARQ-Mac launcher — starts the Electron app pointed at the local sidecar.
# The Python sidecar itself runs as a launchd service (com.barq.mac.sidecar).
set -e
cd "$(dirname "$0")/.."

# Local sidecar is already running via launchd on :8956 — don't spawn another.
export SIDECAR_AUTO_REMOTE=false   # Mac build: local sidecar + LAN host for heavy API
export SIDECAR_REMOTE_URL="${SIDECAR_REMOTE_URL:-http://sai-prabhat-HP-All-in-One-22-dd0xxx.local:8956}"
export BARQ_DEV_TRIGGERS=1          # dev: enables POST /voice/simulate-wake for latency runs

exec npx electron-vite dev
