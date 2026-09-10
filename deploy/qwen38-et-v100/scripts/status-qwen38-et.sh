#!/usr/bin/env bash
set -u
KEY_FILE="${KEY_FILE:-$HOME/.config/1cat-vllm/api-key}"
PORT="${PORT:-18107}"
echo '--- units ---'
systemctl --user is-active qwen38-v100-tp2-lmcache qwen38-v100-tp2-vllm || true
echo '--- containers ---'
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep qwen38-v100-tp2 || echo none
echo '--- gpu 4-7 (TP4) ---'
nvidia-smi --query-gpu=index,name,memory.used --format=csv,noheader | sed -n '5,8p'
echo '--- endpoint ---'
curl -fsS -H "Authorization: Bearer $(<"$KEY_FILE")" "http://127.0.0.1:${PORT}/v1/models" 2>/dev/null && echo || echo 'endpoint DOWN'
echo '--- spec decode (recent) ---'
docker logs --tail 2000 qwen38-v100-tp2-vllm 2>&1 | grep -i 'accept' | tail -2 || true
