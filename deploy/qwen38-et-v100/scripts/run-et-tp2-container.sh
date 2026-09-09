#!/usr/bin/env bash
# qwen38-et-v100: vLLM production container (GPU4+GPU5 TP2, native MTP2).
# Thin wrapper delegating to the generic ai-router runner. All parameters
# come from ../env/qwen38-et-w4a16-mtp2.env (systemd EnvironmentFile).
# Integration map, caveats and rollback: see ../README.md
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DEPLOY="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
exec "$REPO_DEPLOY/ai-router/scripts/run-qwen38-v100-tp2-container.sh"
