#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# Start a local vllm server for VideoARM.
#
# The server exposes an OpenAI-compatible API at:
#   http://localhost:8000/v1
#
# Requirements:
#   pip install vllm
#   CUDA GPU with enough VRAM:
#     Qwen3.6-35B-A3B-FP8 (MoE, ~35 GB FP8 weights) → 48+ GB GPU
#
# Usage:
#   bash start_server.sh           # default model
#   MODEL=Qwen/Qwen3-8B bash start_server.sh   # smaller model for testing
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3.6-35B-A3B-FP8}"
PORT="${PORT:-8000}"
GPU_MEM="${GPU_MEM:-0.90}"
MAX_LEN="${MAX_LEN:-32768}"

echo "Starting vllm server"
echo "  Model   : $MODEL"
echo "  Port    : $PORT"
echo "  GPU mem : ${GPU_MEM}"
echo ""

exec vllm serve "$MODEL" \
    --port "$PORT" \
    --gpu-memory-utilization "$GPU_MEM" \
    --max-model-len "$MAX_LEN" \
    --trust-remote-code \
    --enable-auto-tool-choice \
    --tool-call-parser hermes
