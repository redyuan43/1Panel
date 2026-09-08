#!/usr/bin/env bash
set -euo pipefail

SERVER_BIN="${SERVER_BIN:-$HOME/venvs/1cat-vllm-1.5.0/bin/vllm}"
MODEL_PATH="${MODEL_PATH:-$HOME/model-sources/modelscope/QUASAR-QAT/Qwen3.8-27B-QUASAR-NVFP4}"
DRAFT_MODEL_PATH="${DRAFT_MODEL_PATH:-}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-QUASAR-QAT/Qwen3.8-27B-QUASAR-NVFP4}"
GPU_UUIDS="${GPU_UUIDS:-GPU-008619db-68ab-5b8f-bf82-8fcd838af68b,GPU-cc874591-ffb9-839d-8f63-23e8613bc0e6}"
PIPELINE_PARALLEL_SIZE="${PIPELINE_PARALLEL_SIZE:-1}"
TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE:-2}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18107}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-196608}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e5m2}"
SPECULATIVE_CONFIG="${SPECULATIVE_CONFIG:-}"
LMCACHE_ENABLED="${LMCACHE_ENABLED:-0}"
LMCACHE_SERVER_URL="${LMCACHE_SERVER_URL:-}"
KV_TRANSFER_CONFIG="${KV_TRANSFER_CONFIG:-}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-FLASH_ATTN_V100}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
DISABLE_CUSTOM_ALL_REDUCE="${DISABLE_CUSTOM_ALL_REDUCE:-0}"
API_KEY_FILE="${API_KEY_FILE:-$HOME/.config/1cat-vllm/api-key}"

if [[ ! -x "$SERVER_BIN" ]]; then
    printf 'vLLM executable is missing: %s\n' "$SERVER_BIN" >&2
    exit 1
fi
server_python="$(dirname "$SERVER_BIN")/python"
if [[ ! -x "$server_python" ]]; then
    printf 'vLLM Python executable is missing: %s\n' "$server_python" >&2
    exit 1
fi
if [[ ! -f "$MODEL_PATH/config.json" ]]; then
    printf 'model config is missing: %s/config.json\n' "$MODEL_PATH" >&2
    exit 1
fi
if [[ -n "$DRAFT_MODEL_PATH" && ! -f "$DRAFT_MODEL_PATH/config.json" ]]; then
    printf 'draft model config is missing: %s/config.json\n' "$DRAFT_MODEL_PATH" >&2
    exit 1
fi

IFS=',' read -r -a gpu_uuids <<<"$GPU_UUIDS"
world_size=$((PIPELINE_PARALLEL_SIZE * TENSOR_PARALLEL_SIZE))
if [[ "${#gpu_uuids[@]}" -ne "$world_size" ]]; then
    printf 'GPU_UUIDS count must equal PP x TP (%s)\n' "$world_size" >&2
    exit 1
fi
for uuid in "${gpu_uuids[@]}"; do
    name="$(
        nvidia-smi \
            --id="$uuid" \
            --query-gpu=name \
            --format=csv,noheader 2>/dev/null
    )"
    if [[ "$name" != *V100* && "$name" != *PG500-216* ]]; then
        printf 'refusing non-V100 device %s (%s)\n' "$uuid" "$name" >&2
        exit 1
    fi
done

if [[ "$TENSOR_PARALLEL_SIZE" == "2" && "$MODEL_PATH" == *Qwen3.8*NVFP4* ]]; then
    "$server_python" - <<'PY'
import ast
import importlib.util
from pathlib import Path

spec = importlib.util.find_spec("vllm")
if spec is None or spec.submodule_search_locations is None:
    raise SystemExit("unable to locate the installed vLLM package")
source = (
    Path(next(iter(spec.submodule_search_locations)))
    / "model_executor/layers/quantization/sm70_turbomind.py"
)
tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
alignment = None
for node in tree.body:
    if (
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "NVFP4_OUTPUT_ALIGNMENT"
            for target in node.targets
        )
    ):
        alignment = ast.literal_eval(node.value)
        break
if alignment != 32:
    raise SystemExit(
        f"Qwen3.8 NVFP4 TP2 requires NVFP4_OUTPUT_ALIGNMENT=32, got {alignment!r}"
    )
PY
fi

if [[ "$LMCACHE_ENABLED" == "1" ]]; then
    if [[ -z "$LMCACHE_SERVER_URL" || -z "$KV_TRANSFER_CONFIG" ]]; then
        printf 'LMCache requires LMCACHE_SERVER_URL and KV_TRANSFER_CONFIG\n' >&2
        exit 1
    fi
    "$server_python" - "$KV_TRANSFER_CONFIG" "$LMCACHE_SERVER_URL" <<'PY'
import importlib
import json
import sys
from urllib.parse import urlparse

value = json.loads(sys.argv[1])
server_url = urlparse(sys.argv[2])
expected_module = "lmcache.integration.vllm.lmcache_mp_connector"
if value.get("kv_connector") != "LMCacheMPConnector":
    raise SystemExit("LMCache requires LMCacheMPConnector")
if value.get("kv_role") != "kv_both":
    raise SystemExit("LMCache requires kv_role=kv_both")
if value.get("kv_connector_module_path") != expected_module:
    raise SystemExit("LMCache external connector module is required")
extra = value.get("kv_connector_extra_config")
if not isinstance(extra, dict):
    raise SystemExit("LMCache connector extra config is required")
expected_host = f"tcp://{server_url.hostname}"
if (
    server_url.scheme != "tcp"
    or server_url.hostname not in {"127.0.0.1", "localhost"}
    or server_url.port is None
    or extra.get("lmcache.mp.host") != expected_host
    or int(extra.get("lmcache.mp.port", 0)) != server_url.port
    or float(extra.get("lmcache.mp.heartbeat_interval", 0)) != 10
    or extra.get("lmcache.mp.server_urls")
):
    raise SystemExit(
        "KV_TRANSFER_CONFIG must match the configured local LMCache server"
    )
module = importlib.import_module(expected_module)
if not hasattr(module, "LMCacheMPConnector"):
    raise SystemExit("LMCacheMPConnector is unavailable")
PY
elif [[ -n "$KV_TRANSFER_CONFIG" ]]; then
    printf 'KV_TRANSFER_CONFIG must be empty when LMCache is disabled\n' >&2
    exit 1
fi

export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-$GPU_UUIDS}"
export VLLM_CACHE_ROOT="${VLLM_CACHE_ROOT:-$HOME/.cache/1cat-vllm-1.5.0}"
export VLLM_WORKER_MULTIPROC_METHOD="${VLLM_WORKER_MULTIPROC_METHOD:-spawn}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"

if [[ -s "$API_KEY_FILE" ]]; then
    export VLLM_API_KEY
    VLLM_API_KEY="$(<"$API_KEY_FILE")"
fi

args=(
    serve "$MODEL_PATH"
    --served-model-name "$SERVED_MODEL_NAME"
    --trust-remote-code
    --dtype half
    --pipeline-parallel-size "$PIPELINE_PARALLEL_SIZE"
    --tensor-parallel-size "$TENSOR_PARALLEL_SIZE"
    --distributed-executor-backend mp
    --attention-backend "$ATTENTION_BACKEND"
    --kv-cache-dtype "$KV_CACHE_DTYPE"
    --max-model-len "$MAX_MODEL_LEN"
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
    --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
    --max-num-seqs "$MAX_NUM_SEQS"
    --enable-chunked-prefill
    --enable-prefix-caching
    --enable-prompt-tokens-details
    --mamba-cache-mode align
    --enable-auto-tool-choice
    --tool-call-parser qwen3_coder
    --reasoning-parser qwen3
    --default-chat-template-kwargs '{"enable_thinking":false}'
    --host "$HOST"
    --port "$PORT"
)
if [[ "$ENFORCE_EAGER" == "1" ]]; then
    args+=(--enforce-eager)
fi
if [[ "$DISABLE_CUSTOM_ALL_REDUCE" == "1" ]]; then
    args+=(--disable-custom-all-reduce)
fi
if [[ -n "$SPECULATIVE_CONFIG" ]]; then
    args+=(--speculative-config "$SPECULATIVE_CONFIG")
fi
if [[ "$LMCACHE_ENABLED" == "1" ]]; then
    args+=(--kv-transfer-config "$KV_TRANSFER_CONFIG")
fi

exec "$SERVER_BIN" "${args[@]}"
