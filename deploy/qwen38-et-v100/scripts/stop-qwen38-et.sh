#!/usr/bin/env bash
set -euo pipefail
systemctl --user stop qwen38-v100-tp2-vllm qwen38-v100-tp2-lmcache
sleep 2
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep qwen38-v100-tp2 || echo 'containers stopped'
