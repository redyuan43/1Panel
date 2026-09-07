#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-qwen38-v100-tp2-vllm}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-docker.io/nvidia/cuda@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66}"
GPU_UUIDS="${GPU_UUIDS:-GPU-008619db-68ab-5b8f-bf82-8fcd838af68b,GPU-cc874591-ffb9-839d-8f63-23e8613bc0e6}"
PIPELINE_PARALLEL_SIZE="${PIPELINE_PARALLEL_SIZE:-1}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"
PYTHON_RUNTIME="${PYTHON_RUNTIME:-$HOME/runtimes/python-3.12.14-20260901}"
BASE_VLLM_VENV="${BASE_VLLM_VENV:-${VLLM_VENV:-$HOME/venvs/1cat-vllm-1.5.0}}"
LMCACHE_VENV="${LMCACHE_VENV:-$HOME/venvs/1cat-vllm-1.5.0-lmcache}"
MODEL_PATH="${MODEL_PATH:-$HOME/model-sources/modelscope/QUASAR-QAT/Qwen3.8-27B-QUASAR-NVFP4}"
DRAFT_MODEL_PATH="${DRAFT_MODEL_PATH:-}"
RUNTIME_CONFIG_DIR="${RUNTIME_CONFIG_DIR:-$HOME/.config/1cat-vllm}"
VLLM_CACHE_ROOT="${VLLM_CACHE_ROOT:-$HOME/.cache/1cat-vllm-1.5.0}"
CONTAINER_HOME="${CONTAINER_HOME:-$VLLM_CACHE_ROOT/home}"
INNER_SCRIPT="${INNER_SCRIPT:-/home/ai/github/1Panel/deploy/ai-router/scripts/run-qwen38-v100-tp2-vllm.sh}"
SPECULATIVE_CONFIG="${SPECULATIVE_CONFIG:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LMCACHE_RESOLVER="${LMCACHE_RESOLVER:-$SCRIPT_DIR/resolve-qwen38-v100-tp2-lmcache.py}"
LMCACHE_CHECK="${LMCACHE_CHECK:-$SCRIPT_DIR/check-qwen38-v100-tp2-lmcache.py}"
AI_ROUTER_DEFAULTS_PATH="${AI_ROUTER_DEFAULTS_PATH:-$SCRIPT_DIR/../config/defaults.yaml}"
AI_ROUTER_RUNTIME_SETTINGS_PATH="${AI_ROUTER_RUNTIME_SETTINGS_PATH:-/opt/1panel/ai-router/settings.yaml}"

resolver_args=(
    "$LMCACHE_RESOLVER"
    --defaults "$AI_ROUTER_DEFAULTS_PATH"
    --runtime "$AI_ROUTER_RUNTIME_SETTINGS_PATH"
)
if [[ -n "${LMCACHE_SERVER_URL:-}" ]]; then
    resolver_args+=(--server-url "$LMCACHE_SERVER_URL")
fi
mapfile -t lmcache_values < <(
    "$BASE_VLLM_VENV/bin/python" "${resolver_args[@]}"
)
if [[ "${#lmcache_values[@]}" -ne 11 ]]; then
    printf 'LMCache resolver returned an invalid field count\n' >&2
    exit 1
fi
LMCACHE_ENABLED="${LMCACHE_ENABLED:-${lmcache_values[0]}}"
LMCACHE_SERVER_URL="${LMCACHE_SERVER_URL:-${lmcache_values[1]}}"
LMCACHE_HTTP_URL="${LMCACHE_HTTP_URL:-${lmcache_values[2]}}"
if [[ "$LMCACHE_ENABLED" != "0" && "$LMCACHE_ENABLED" != "1" ]]; then
    printf 'LMCACHE_ENABLED must be 0 or 1\n' >&2
    exit 1
fi
if [[ "$LMCACHE_ENABLED" == "1" ]]; then
    VLLM_VENV="$LMCACHE_VENV"
    SERVER_BIN="${LMCACHE_SERVER_BIN:-$LMCACHE_VENV/bin/vllm}"
    KV_TRANSFER_CONFIG="${KV_TRANSFER_CONFIG:-${lmcache_values[10]}}"
    "$BASE_VLLM_VENV/bin/python" "$LMCACHE_CHECK" \
        --defaults "$AI_ROUTER_DEFAULTS_PATH" \
        --runtime "$AI_ROUTER_RUNTIME_SETTINGS_PATH" \
        --timeout "${LMCACHE_START_TIMEOUT:-180}"
else
    VLLM_VENV="$BASE_VLLM_VENV"
    SERVER_BIN="${SERVER_BIN:-$BASE_VLLM_VENV/bin/vllm}"
    if [[ -n "${KV_TRANSFER_CONFIG:-}" ]]; then
        printf 'KV_TRANSFER_CONFIG must be empty when LMCache is disabled\n' >&2
        exit 1
    fi
    KV_TRANSFER_CONFIG=""
fi

if ! docker info >/dev/null 2>&1; then
    printf 'Docker daemon is unavailable\n' >&2
    exit 1
fi
for path in "$PYTHON_RUNTIME" "$VLLM_VENV" "$MODEL_PATH"; do
    if [[ ! -d "$path" ]]; then
        printf 'required directory is missing: %s\n' "$path" >&2
        exit 1
    fi
done
if [[ -n "$DRAFT_MODEL_PATH" && ! -d "$DRAFT_MODEL_PATH" ]]; then
    printf 'draft model directory is missing: %s\n' "$DRAFT_MODEL_PATH" >&2
    exit 1
fi
if [[ ! -x "$INNER_SCRIPT" ]]; then
    printf 'inner startup script is missing: %s\n' "$INNER_SCRIPT" >&2
    exit 1
fi
if (( PIPELINE_PARALLEL_SIZE < 1 || TENSOR_PARALLEL_SIZE < 1 )); then
    printf 'parallel sizes must be positive integers\n' >&2
    exit 1
fi

world_size=$((PIPELINE_PARALLEL_SIZE * TENSOR_PARALLEL_SIZE))
IFS=',' read -r -a gpu_uuids <<<"$GPU_UUIDS"
if [[ "${#gpu_uuids[@]}" -ne "$world_size" ]]; then
    printf 'GPU_UUIDS count must equal PP x TP (%s)\n' "$world_size" >&2
    exit 1
fi
cuda_visible_devices="$(
    seq -s, 0 $((world_size - 1))
)"

mkdir -p "$RUNTIME_CONFIG_DIR" "$VLLM_CACHE_ROOT"
mkdir -p \
    "$VLLM_CACHE_ROOT/cuda" \
    "$VLLM_CACHE_ROOT/numba" \
    "$VLLM_CACHE_ROOT/torch_extensions" \
    "$VLLM_CACHE_ROOT/torchinductor" \
    "$VLLM_CACHE_ROOT/triton" \
    "$CONTAINER_HOME"

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    printf 'container already exists: %s\n' "$CONTAINER_NAME" >&2
    exit 1
fi

docker_args=(
    run
    --rm
    --name "$CONTAINER_NAME"
    --runtime nvidia
    --network host
    --ipc host
    --ulimit memlock=-1
    --cap-add IPC_LOCK
    --security-opt no-new-privileges
    --stop-timeout 300
    --user "$(id -u):$(id -g)"
    -e HOME="$CONTAINER_HOME"
    -e NVIDIA_VISIBLE_DEVICES="$GPU_UUIDS"
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    -e GPU_UUIDS="$GPU_UUIDS"
    -e CUDA_VISIBLE_DEVICES="$cuda_visible_devices"
    -e PIPELINE_PARALLEL_SIZE="$PIPELINE_PARALLEL_SIZE"
    -e TENSOR_PARALLEL_SIZE="$TENSOR_PARALLEL_SIZE"
    -e SERVER_BIN="$SERVER_BIN"
    -e MODEL_PATH="$MODEL_PATH"
    -e DRAFT_MODEL_PATH="$DRAFT_MODEL_PATH"
    -e SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-QUASAR-QAT/Qwen3.8-27B-QUASAR-NVFP4}"
    -e HOST="${HOST:-127.0.0.1}"
    -e PORT="${PORT:-18107}"
    -e MAX_MODEL_LEN="${MAX_MODEL_LEN:-196608}"
    -e MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
    -e MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-4096}"
    -e GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
    -e KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e5m2}"
    -e SPECULATIVE_CONFIG="$SPECULATIVE_CONFIG"
    -e LMCACHE_ENABLED="$LMCACHE_ENABLED"
    -e LMCACHE_SERVER_URL="$LMCACHE_SERVER_URL"
    -e LMCACHE_HTTP_URL="$LMCACHE_HTTP_URL"
    -e KV_TRANSFER_CONFIG="$KV_TRANSFER_CONFIG"
    -e ATTENTION_BACKEND="${ATTENTION_BACKEND:-FLASH_ATTN_V100}"
    -e ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
    -e DISABLE_CUSTOM_ALL_REDUCE="${DISABLE_CUSTOM_ALL_REDUCE:-0}"
    -e NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-}"
    -e VLLM_SM70_QUANT_BACKEND="${VLLM_SM70_QUANT_BACKEND:-auto}"
    -e VLLM_USE_NVFP4_CT_EMULATIONS="${VLLM_USE_NVFP4_CT_EMULATIONS:-0}"
    -e VLLM_BATCH_INVARIANT="${VLLM_BATCH_INVARIANT:-0}"
    -e API_KEY_FILE="${API_KEY_FILE:-/home/ai/.config/1cat-vllm/api-key}"
    -e VLLM_CACHE_ROOT="$VLLM_CACHE_ROOT"
    -e CUDA_CACHE_PATH="$VLLM_CACHE_ROOT/cuda"
    -e FLASHINFER_WORKSPACE_BASE="$CONTAINER_HOME"
    -e NUMBA_CACHE_DIR="$VLLM_CACHE_ROOT/numba"
    -e TORCH_EXTENSIONS_DIR="$VLLM_CACHE_ROOT/torch_extensions"
    -e TORCHINDUCTOR_CACHE_DIR="$VLLM_CACHE_ROOT/torchinductor"
    -e TRITON_CACHE_DIR="$VLLM_CACHE_ROOT/triton"
    -e XDG_CACHE_HOME="$VLLM_CACHE_ROOT"
    -e HF_HUB_OFFLINE=1
    -e TRANSFORMERS_OFFLINE=1
    -v "$PYTHON_RUNTIME:$PYTHON_RUNTIME:ro"
    -v "$VLLM_VENV:$VLLM_VENV:ro"
    -v "$MODEL_PATH:$MODEL_PATH:ro"
    -v "$RUNTIME_CONFIG_DIR:$RUNTIME_CONFIG_DIR:ro"
    -v "$VLLM_CACHE_ROOT:$VLLM_CACHE_ROOT"
    -v "$INNER_SCRIPT:$INNER_SCRIPT:ro"
)
if [[ -n "${VLLM_NVFP4_GEMM_BACKEND:-}" ]]; then
    docker_args+=(-e VLLM_NVFP4_GEMM_BACKEND="$VLLM_NVFP4_GEMM_BACKEND")
fi
if [[ -n "$DRAFT_MODEL_PATH" ]]; then
    docker_args+=(-v "$DRAFT_MODEL_PATH:$DRAFT_MODEL_PATH:ro")
fi

exec docker "${docker_args[@]}" "$CONTAINER_IMAGE" "$INNER_SCRIPT"
