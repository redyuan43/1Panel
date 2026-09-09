#!/usr/bin/env bash
# qwen38-et-v100: LMCache DRAM L1 sidecar. Thin wrapper delegating to the
# generic ai-runner script (config resolved from ai-router settings).
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DEPLOY="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
exec "$REPO_DEPLOY/ai-router/scripts/run-qwen38-v100-tp2-lmcache-container.sh"
