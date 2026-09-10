#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${LMCACHE_RESOLVER:-$SCRIPT_DIR/resolve-qwen38-v100-tp2-lmcache.py}"
DEFAULTS_PATH="${AI_ROUTER_DEFAULTS_PATH:-$SCRIPT_DIR/../config/defaults.yaml}"
RUNTIME_SETTINGS_PATH="${AI_ROUTER_RUNTIME_SETTINGS_PATH:-/opt/1panel/ai-router/settings.yaml}"
BASE_VLLM_VENV="${BASE_VLLM_VENV:-$HOME/venvs/1cat-vllm-1.5.0}"
LMCACHE_VENV="${LMCACHE_VENV:-$HOME/venvs/1cat-vllm-1.5.0-lmcache}"
PYTHON_RUNTIME="${PYTHON_RUNTIME:-$HOME/runtimes/python-3.12.14-20260901}"
CONTAINER_NAME="${LMCACHE_CONTAINER_NAME:-qwen38-v100-tp2-lmcache}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-docker.io/nvidia/cuda@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66}"
GPU_UUIDS="${GPU_UUIDS:-GPU-008619db-68ab-5b8f-bf82-8fcd838af68b,GPU-cc874591-ffb9-839d-8f63-23e8613bc0e6}"
LMCACHE_CACHE_ROOT="${LMCACHE_CACHE_ROOT:-$HOME/.cache/1cat-vllm-1.5.0-lmcache}"
CONTAINER_HOME="${LMCACHE_CONTAINER_HOME:-$LMCACHE_CACHE_ROOT/home}"
NUMBA_CACHE_DIR="${LMCACHE_NUMBA_CACHE_DIR:-$LMCACHE_CACHE_ROOT/numba}"
CUDA_CACHE_PATH="${LMCACHE_CUDA_CACHE_PATH:-$LMCACHE_CACHE_ROOT/cuda}"
IFS=, read -r -a gpu_uuid_array <<< "$GPU_UUIDS"
gpu_count="${#gpu_uuid_array[@]}"
gpu_worker_count="${LMCACHE_MAX_GPU_WORKERS:-$gpu_count}"
if [[ ! "$gpu_worker_count" =~ ^[1-9][0-9]*$ ]]; then
    printf 'LMCACHE_MAX_GPU_WORKERS must be a positive integer\n' >&2
    exit 1
fi
cuda_visible_devices="$(seq -s, 0 "$((gpu_count - 1))")"

resolver_args=(
    "$RESOLVER"
    --defaults "$DEFAULTS_PATH"
    --runtime "$RUNTIME_SETTINGS_PATH"
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
enabled="${LMCACHE_ENABLED:-${lmcache_values[0]}}"
server_host="${lmcache_values[3]}"
server_port="${lmcache_values[4]}"
l1_size_gb="${LMCACHE_L1_SIZE_GB:-${lmcache_values[5]}}"
memory_max_gb="${LMCACHE_MEMORY_MAX_GB:-${lmcache_values[6]}}"
chunk_size="${LMCACHE_CHUNK_SIZE:-${lmcache_values[7]}}"
numa_node="${LMCACHE_NUMA_NODE:-${lmcache_values[8]}}"
separate_object_groups="${lmcache_values[9]}"
http_url="${LMCACHE_HTTP_URL:-${lmcache_values[2]}}"

if [[ "$enabled" != "0" && "$enabled" != "1" ]]; then
    printf 'LMCACHE_ENABLED must be 0 or 1\n' >&2
    exit 1
fi
if [[ "$enabled" != "1" ]]; then
    printf 'LMCache is disabled in persisted settings; keeping dependency active without allocating cache memory\n'
    exec /usr/bin/sleep infinity
fi
if [[ "$memory_max_gb" != "96" ]]; then
    printf 'LMCache memory limit must remain 96 GiB\n' >&2
    exit 1
fi
if [[ "$separate_object_groups" != "1" ]]; then
    printf 'LMCache separate object groups must remain enabled\n' >&2
    exit 1
fi
if [[ ! -x "$LMCACHE_VENV/bin/python" ]]; then
    printf 'LMCache environment is missing: %s\n' "$LMCACHE_VENV" >&2
    exit 1
fi
if [[ ! -d "$PYTHON_RUNTIME" ]]; then
    printf 'Python runtime is missing: %s\n' "$PYTHON_RUNTIME" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    printf 'Docker daemon is unavailable\n' >&2
    exit 1
fi
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    printf 'container already exists: %s\n' "$CONTAINER_NAME" >&2
    exit 1
fi
mkdir -p \
    "$LMCACHE_CACHE_ROOT" \
    "$CONTAINER_HOME" \
    "$NUMBA_CACHE_DIR" \
    "$CUDA_CACHE_PATH"

http_host="${http_url#http://}"
http_host="${http_host%%:*}"
http_port="${http_url##*:}"
if [[ "$http_host" != "127.0.0.1" && "$http_host" != "localhost" ]]; then
    printf 'LMCache HTTP endpoint must remain loopback-only\n' >&2
    exit 1
fi
if [[ "$server_host" != "127.0.0.1" && "$server_host" != "localhost" ]]; then
    printf 'LMCache message endpoint must remain loopback-only\n' >&2
    exit 1
fi
numa_cpu_list_path="/sys/devices/system/node/node${numa_node}/cpulist"
if [[ ! -r "$numa_cpu_list_path" ]]; then
    printf 'NUMA CPU list is unavailable: %s\n' "$numa_cpu_list_path" >&2
    exit 1
fi
numa_cpu_list="$(<"$numa_cpu_list_path")"
if [[ -z "$numa_cpu_list" ]]; then
    printf 'NUMA node %s has no CPUs\n' "$numa_node" >&2
    exit 1
fi

exec docker run \
    --rm \
    --name "$CONTAINER_NAME" \
    --runtime nvidia \
    --network host \
    --ipc host \
    --cpuset-cpus "$numa_cpu_list" \
    --cpuset-mems "$numa_node" \
    --memory "${memory_max_gb}g" \
    --memory-swap "${memory_max_gb}g" \
    --ulimit memlock=-1 \
    --cap-add IPC_LOCK \
    --security-opt no-new-privileges \
    --stop-timeout 120 \
    --user "$(id -u):$(id -g)" \
    -e HOME="$CONTAINER_HOME" \
    -e NVIDIA_VISIBLE_DEVICES="$GPU_UUIDS" \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    -e CUDA_VISIBLE_DEVICES="$cuda_visible_devices" \
    -e HF_HUB_OFFLINE=1 \
    -e TRANSFORMERS_OFFLINE=1 \
    -e LMCACHE_CUDA_MAJOR=12 \
    -e NUMBA_CACHE_DIR="$NUMBA_CACHE_DIR" \
    -e CUDA_CACHE_PATH="$CUDA_CACHE_PATH" \
    -e XDG_CACHE_HOME="$LMCACHE_CACHE_ROOT" \
    -v "$PYTHON_RUNTIME:$PYTHON_RUNTIME:ro" \
    -v "$LMCACHE_VENV:$LMCACHE_VENV:ro" \
    -v "$LMCACHE_CACHE_ROOT:$LMCACHE_CACHE_ROOT" \
    "$CONTAINER_IMAGE" \
    "$LMCACHE_VENV/bin/python" -m lmcache.cli.main server \
    --host "$server_host" \
    --port "$server_port" \
    --http-host "$http_host" \
    --http-port "$http_port" \
    --instance-id qwen38-v100-tp2-lmcache \
    --chunk-size "$chunk_size" \
    --l1-size-gb "$l1_size_gb" \
    --no-l1-use-lazy \
    --max-workers 4 \
    --max-gpu-workers "$gpu_worker_count" \
    --max-cpu-workers 4 \
    --hash-algorithm blake3 \
    --engine-type default \
    --supported-transfer-mode lmcache_driven \
    --eviction-policy LRU \
    --separate-object-groups \
    --worker-reap-timeout-seconds 120 \
    --worker-registration-grace-seconds 1800
