#!/usr/bin/env bash
# Start two vllm servers for VideoARM — both co-located on a single GPU (GPU 0).
# LLM  (Qwen/Qwen3.6-35B-A3B-FP8) — GPU 0, tensor-parallel=1  port 8000
# ASR  (Qwen/Qwen3-ASR-1.7B)      — GPU 0, tensor-parallel=1  port 8001
#
# Fits on one A100 80GB: FP8 LLM (~35 GB weights + KV cache) + ASR (~4 GB).
# The LLM is started first and we wait for it to become healthy before
# launching ASR, so each vllm process profiles the real free memory and the
# two memory budgets do not collide.
#
# Usage: bash start_servers.sh
set -euo pipefail

LLM_MODEL="${LLM_MODEL:-Qwen/Qwen3.6-35B-A3B-FP8}"
ASR_MODEL="${ASR_MODEL:-Qwen/Qwen3-ASR-1.7B}"
LLM_PORT="${LLM_PORT:-8000}"
ASR_PORT="${ASR_PORT:-8001}"
GPU="${GPU:-0}"
# Memory split on the shared GPU (fraction of total VRAM per vllm process).
LLM_GPU_MEM="${LLM_GPU_MEM:-0.70}"   # ~56 GB on an 80 GB card
ASR_GPU_MEM="${ASR_GPU_MEM:-0.12}"   # ~10 GB

# Absolute path to this script's directory — must be absolute so the venv bin
# we add to PATH still resolves inside vllm's spawned worker subprocesses
# (which run from a different cwd).
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HERE/logs"
LOG="$HERE/logs"

# Invoke vllm from the project venv (not via `uv run`, which would re-sync the
# environment and uninstall vllm — it is not a declared project dependency).
VLLM="${VLLM:-$HERE/.venv/bin/vllm}"

# Use the system default C compiler for Triton's import-time JIT build.
# (Override CC/CXX in the environment if a specific gcc is needed.)
export CC="${CC:-gcc}" CXX="${CXX:-g++}"

# We invoke vllm via the venv binary without activating the venv, so put the
# venv's bin on PATH (FlashInfer shells out to `ninja` by name during its JIT
# build) and point CUDA_HOME at the CUDA toolkit bundled in the nvidia cu13
# wheel (FlashInfer/Triton need nvcc + CUDA headers to compile kernels).
SITE_PKGS="$("$HERE/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
export PATH="$HERE/.venv/bin:$PATH"
CUDA_HOME_CANDIDATE="$SITE_PKGS/nvidia/cu13"
if [ -x "$CUDA_HOME_CANDIDATE/bin/nvcc" ]; then
    export CUDA_HOME="$CUDA_HOME_CANDIDATE"
    export PATH="$CUDA_HOME/bin:$PATH"
    echo "  CUDA_HOME: $CUDA_HOME (nvcc $("$CUDA_HOME/bin/nvcc" --version | sed -n 's/.*release //p'))"
fi

# Skip FlashInfer's runtime-JIT sampler: its bundled CCCL headers are
# incompatible with the cu13 nvcc, and the build fails. vLLM falls back to its
# native PyTorch top-p/top-k sampler, which is fine for this pipeline
# (generation runs at temperature 0–0.1).
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

echo "Starting LLM server (port $LLM_PORT, GPU $GPU, tp=1, mem=$LLM_GPU_MEM)..."
CUDA_VISIBLE_DEVICES="$GPU" "$VLLM" serve "$LLM_MODEL" \
    --port "$LLM_PORT" --tensor-parallel-size 1 \
    --gpu-memory-utilization "$LLM_GPU_MEM" --max-model-len 32768 \
    --trust-remote-code --enable-auto-tool-choice --tool-call-parser hermes \
    --served-model-name "$LLM_MODEL" \
    --mm-processor-cache-gb 0 > "$LOG/llm.log" 2>&1 &
LLM_PID=$!

echo "Waiting for LLM server to be healthy before starting ASR..."
until curl -sf "http://localhost:$LLM_PORT/health" >/dev/null 2>&1; do
    if ! kill -0 "$LLM_PID" 2>/dev/null; then
        echo "LLM server died during startup — see $LOG/llm.log" >&2
        exit 1
    fi
    sleep 10
done
echo "LLM server ready."

echo "Starting ASR server (port $ASR_PORT, GPU $GPU, tp=1, mem=$ASR_GPU_MEM)..."
# Cap max-model-len: the model defaults to 65536 (needs ~7 GiB KV cache), far
# more than audio transcription requires. 8192 keeps KV cache under ~1 GiB so it
# fits in the ASR memory slice on the shared GPU.
ASR_MAX_LEN="${ASR_MAX_LEN:-8192}"
CUDA_VISIBLE_DEVICES="$GPU" "$VLLM" serve "$ASR_MODEL" \
    --port "$ASR_PORT" --tensor-parallel-size 1 \
    --gpu-memory-utilization "$ASR_GPU_MEM" --trust-remote-code \
    --max-model-len "$ASR_MAX_LEN" \
    --served-model-name "$ASR_MODEL" > "$LOG/asr.log" 2>&1 &
ASR_PID=$!

echo "LLM PID=$LLM_PID  ASR PID=$ASR_PID"
echo "Logs: $LOG/llm.log  $LOG/asr.log"
echo ""
echo "Waiting for ASR server..."
until curl -sf "http://localhost:$ASR_PORT/health" >/dev/null 2>&1; do
    if ! kill -0 "$ASR_PID" 2>/dev/null; then
        echo "ASR server died during startup — see $LOG/asr.log" >&2
        kill "$LLM_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 10
done
echo "Both servers ready."
trap "kill $LLM_PID $ASR_PID 2>/dev/null" INT TERM
wait
