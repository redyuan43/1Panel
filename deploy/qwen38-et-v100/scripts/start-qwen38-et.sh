#!/usr/bin/env bash
# Start the EfficientThink W4A16 + MTP2 chain and wait for readiness.
set -euo pipefail
KEY_FILE="${KEY_FILE:-$HOME/.config/1cat-vllm/api-key}"
PORT="${PORT:-18107}"
systemctl --user start qwen38-v100-tp2-vllm   # Requires= pulls in lmcache
printf 'waiting for 127.0.0.1:%s ...\n' "$PORT"
for _ in $(seq 1 240); do
    if curl -fsS -H "Authorization: Bearer $(<"$KEY_FILE")" \
        "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
        echo 'READY'
        curl -fsS -H "Authorization: Bearer $(<"$KEY_FILE")" "http://127.0.0.1:${PORT}/v1/models"; echo
        exit 0
    fi
    sleep 5
done
echo 'not ready after 20 minutes' >&2
echo 'logs: journalctl --user -u qwen38-v100-tp2-vllm -e --no-pager' >&2
exit 1
