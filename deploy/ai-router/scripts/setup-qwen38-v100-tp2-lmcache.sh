#!/usr/bin/env bash
set -euo pipefail

BASE_VENV="${BASE_VENV:-$HOME/venvs/1cat-vllm-1.5.0}"
TARGET_VENV="${TARGET_VENV:-$HOME/venvs/1cat-vllm-1.5.0-lmcache}"
SOURCE_DIR="${SOURCE_DIR:-$HOME/src/LMCache-v0.5.4-sm70}"
PYTHON_RUNTIME="${PYTHON_RUNTIME:-$HOME/runtimes/python-3.12.14-20260901}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-docker.io/nvidia/cuda@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66}"
LMCACHE_REPOSITORY="${LMCACHE_REPOSITORY:-https://github.com/LMCache/LMCache.git}"
LMCACHE_TAG="${LMCACHE_TAG:-v0.5.4}"
LMCACHE_COMMIT="${LMCACHE_COMMIT:-3e11b8ed191631e6f098b8038235823f1a410b24}"
BUILD_PROXY="${BUILD_PROXY:-http://127.0.0.1:10808}"

if [[ ! -x "$BASE_VENV/bin/python" ]]; then
    printf 'base vLLM environment is missing: %s\n' "$BASE_VENV" >&2
    exit 1
fi
if [[ -f "$TARGET_VENV/.lmcache-sm70-build" ]]; then
    printf 'LMCache Volta environment is already complete: %s\n' "$TARGET_VENV"
    exit 0
fi
if [[ -e "$TARGET_VENV" && ! -x "$TARGET_VENV/bin/python" ]]; then
    printf 'target path exists but is not a resumable environment: %s\n' "$TARGET_VENV" >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    printf 'Docker daemon is unavailable\n' >&2
    exit 1
fi

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
    git clone --branch "$LMCACHE_TAG" --depth 1 \
        "$LMCACHE_REPOSITORY" "$SOURCE_DIR"
fi
actual_commit="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
if [[ "$actual_commit" != "$LMCACHE_COMMIT" ]]; then
    printf 'LMCache source commit mismatch: %s\n' "$actual_commit" >&2
    exit 1
fi

if [[ ! -e "$TARGET_VENV" ]]; then
    printf 'Copying isolated vLLM environment to %s\n' "$TARGET_VENV"
    cp -a "$BASE_VENV" "$TARGET_VENV"

    "$TARGET_VENV/bin/python" - "$BASE_VENV" "$TARGET_VENV" <<'PY'
from pathlib import Path
import sys

source, target = sys.argv[1:]
bin_dir = Path(target) / "bin"
for path in bin_dir.iterdir():
    if not path.is_file():
        continue
    try:
        raw = path.read_bytes()
    except OSError:
        continue
    prefix = f"#!{source}/bin/".encode()
    if not raw.startswith(prefix):
        continue
    path.write_bytes(raw.replace(source.encode(), target.encode(), 1))
cfg = Path(target) / "pyvenv.cfg"
cfg.write_text(
    cfg.read_text(encoding="utf-8").replace(source, target),
    encoding="utf-8",
)
PY
else
    printf 'Resuming incomplete isolated environment: %s\n' "$TARGET_VENV"
fi

docker run --rm \
    --network host \
    --ipc host \
    --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" \
    -e HOME="$HOME" \
    -e PATH="$TARGET_VENV/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    -e HTTP_PROXY="$BUILD_PROXY" \
    -e HTTPS_PROXY="$BUILD_PROXY" \
    -e NO_PROXY=localhost,127.0.0.0/8,::1 \
    -e TORCH_CUDA_ARCH_LIST=7.0 \
    -e LMCACHE_CUDA_MAJOR=12 \
    -e BUILD_WITH_CUDA=1 \
    -e CC=gcc \
    -e CXX=g++ \
    -e CUDAHOSTCXX=g++ \
    -e ENABLE_CXX11_ABI=1 \
    -e SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.5.4 \
    -e MAX_JOBS="${MAX_JOBS:-2}" \
    -e NVCC_THREADS="${NVCC_THREADS:-2}" \
    -v "$PYTHON_RUNTIME:$PYTHON_RUNTIME:ro" \
    -v "$TARGET_VENV:$TARGET_VENV" \
    -v "$SOURCE_DIR:$SOURCE_DIR" \
    "$CONTAINER_IMAGE" \
    /bin/bash -lc "
        set -euo pipefail
        \"$TARGET_VENV/bin/python\" -m pip install \
          'wheel>=0.45' 'setuptools-scm>=8' \
          'aiofile' 'aiofiles' 'blake3' 'aiohttp' 'awscrt' \
          'cryptography' 'huggingface-hub>=1.5.0' 'msgspec' \
          'numpy==2.3.5' 'numba' 'nvtx' 'cupy-cuda12x' \
          'cufile-python' \
          'opentelemetry-api==1.44.0' \
          'opentelemetry-sdk==1.44.0' \
          'opentelemetry-exporter-otlp==1.44.0' \
          'opentelemetry-exporter-prometheus==0.65b0' \
          'prometheus-client==0.26.0' \
          'psutil' 'py-cpuinfo' 'pytest' 'pyyaml' \
          'pyzmq>=25.0.0' 'redis' 'safetensors' \
          'setuptools>=77.0.3,<81.0.0' 'sortedcontainers' \
          'uvicorn' 'httptools' 'cachetools' 'google-api-core' \
          'google-cloud-bigtable'
        \"$TARGET_VENV/bin/python\" -m pip install \
          --no-build-isolation --no-deps \"$SOURCE_DIR\"
        \"$TARGET_VENV/bin/python\" - <<'PY'
import importlib
import lmcache
import torch
module = importlib.import_module(
    'lmcache.integration.vllm.lmcache_mp_connector'
)
assert hasattr(module, 'LMCacheMPConnector')
assert torch.version.cuda == '12.8'
print('lmcache', getattr(lmcache, '__version__', 'unknown'))
print('torch', torch.__version__, torch.version.cuda)
PY
        so=\"\$(find \"$TARGET_VENV\" -name 'cuda_ops*.so' -print -quit)\"
        test -n \"\$so\"
        cuobjdump --list-elf \"\$so\" | grep -q 'sm_70'
    "

printf '%s\n' \
    "lmcache_tag=$LMCACHE_TAG" \
    "lmcache_commit=$LMCACHE_COMMIT" \
    "torch_cuda=12.8" \
    "cuda_arch=sm_70" \
    >"$TARGET_VENV/.lmcache-sm70-build"
printf 'LMCache Volta environment prepared: %s\n' "$TARGET_VENV"
