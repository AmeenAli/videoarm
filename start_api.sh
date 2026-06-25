#!/usr/bin/env bash
# Start the VideoARM FastAPI server on port 8080.
# Requires the vllm model server to already be running (bash start_servers.sh).
#
# Usage:
#   bash start_api.sh
#   VIDEOARM_API_KEY=mykey bash start_api.sh
set -euo pipefail

export VIDEOARM_API_KEY="${VIDEOARM_API_KEY:-7db96b28bfafe87a851c22000e9758c5}"
export VIDEOARM_OUTPUT_DIR="${VIDEOARM_OUTPUT_DIR:-/tmp/videoarm_jobs}"

# Summarizer concurrency. The vLLM backend batches concurrent requests, so
# running segments and their vision calls in parallel keeps the A100 busy.
# Measured on a 15.5-min/4-segment video: 242s (1x1) → 129s at 3x6 (~1.9x).
# Past this the single-GPU vision model is compute-bound — raise for longer
# videos / more GPU, lower if you hit memory pressure.
export VIDEOARM_SEGMENT_CONCURRENCY="${VIDEOARM_SEGMENT_CONCURRENCY:-3}"
export VIDEOARM_VISUAL_CONCURRENCY="${VIDEOARM_VISUAL_CONCURRENCY:-6}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

HERE="$(cd "$(dirname "$0")" && pwd)"

# yt-dlp needs the Deno JS runtime on PATH to solve YouTube's "n" challenge
# (otherwise only storyboards come back and downloads fail). Deno installs to
# ~/.deno/bin, which a screen/systemd launch doesn't pick up by default.
export PATH="$HOME/.deno/bin:$PATH"

echo "Starting VideoARM API server"
echo "  Host    : $HOST"
echo "  Port    : $PORT"
echo "  API key : ${VIDEOARM_API_KEY:0:8}…"
echo ""

# Run uvicorn from the venv directly — NOT via `uv run`, which re-syncs the
# environment to pyproject/uv.lock and would uninstall vllm (installed manually,
# not a declared dependency), breaking the model servers on their next restart.
exec "$HERE/.venv/bin/uvicorn" videoarm.api.server:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers 1
