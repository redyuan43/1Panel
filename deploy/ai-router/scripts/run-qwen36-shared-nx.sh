#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-$HOME/llama/Qwen3.6-35B-A3B-UD-IQ2_M.gguf}"
MMPROJ_PATH="${MMPROJ_PATH:-$HOME/llama/Cerebellum-mmproj-Q8_0.gguf}"
SERVER_BIN="${SERVER_BIN:-$HOME/llama/llama-server}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8081}"
CTX_SIZE="${CTX_SIZE:-57344}"
API_KEY_FILE="${API_KEY_FILE:-$HOME/.config/qwen36-shared/api-key}"

export CUDA_MODULE_LOADING="${CUDA_MODULE_LOADING:-LAZY}"
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-$HOME/llama/lib}"

install -d -m 700 "$(dirname "$API_KEY_FILE")"
if [[ ! -e "$API_KEY_FILE" ]]; then
    temporary_key="$(mktemp "$(dirname "$API_KEY_FILE")/.api-key.XXXXXX")"
    trap 'rm -f "$temporary_key"' EXIT
    umask 077
    dd if=/dev/urandom bs=48 count=1 status=none \
        | base64 \
        | tr -d '\n' >"$temporary_key"
    chmod 600 "$temporary_key"
    mv "$temporary_key" "$API_KEY_FILE"
    trap - EXIT
fi
if [[ ! -f "$API_KEY_FILE" || ! -s "$API_KEY_FILE" ]]; then
    printf 'Qwen3.6 API key file is missing or empty: %s\n' \
        "$API_KEY_FILE" >&2
    exit 1
fi
chmod 600 "$API_KEY_FILE"

args=(
    -m "$MODEL_PATH"
    --mmproj "$MMPROJ_PATH"
    --image-max-tokens 1024
    --alias siyuan/qwen36-shared
    --jinja
    -c "$CTX_SIZE"
    -fa on
    -ctk q4_0
    -ctv q4_0
    -ngl 99
    -b 512
    -ub 256
    --parallel 1
    --cache-ram 0
    --ctx-checkpoints 2
    --spec-type draft-mtp
    --spec-draft-n-max 2
    --temp 1.0
    --top-p 0.95
    --top-k 20
    --min-p 0.0
    --host "$HOST"
    --port "$PORT"
    --api-key-file "$API_KEY_FILE"
)

if [[ -n "${CACHE_SLOT_SAVE_PATH:-}" ]]; then
    install -d -m 700 "$CACHE_SLOT_SAVE_PATH"
    args+=(--slot-save-path "$CACHE_SLOT_SAVE_PATH")
fi

exec "$SERVER_BIN" "${args[@]}"
