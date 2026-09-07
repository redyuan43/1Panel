#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--apply" ]]; then
    printf '%s\n' \
        "Dry run only." \
        "This rollback sets lmcache.enabled=false in /opt/1panel/ai-router/settings.yaml" \
        "and restarts the LMCache/TP2 units with the existing TP2/MTP2/APC configuration." \
        "Run with --apply only during an authorized TP2 restart window."
    exit 0
fi

SETTINGS_PATH="${AI_ROUTER_RUNTIME_SETTINGS_PATH:-/opt/1panel/ai-router/settings.yaml}"
PYTHON="${BASE_VLLM_VENV:-$HOME/venvs/1cat-vllm-1.5.0}/bin/python"
backup="$SETTINGS_PATH.lmcache-rollback-$(date +%Y%m%dT%H%M%S)"
if [[ -w "$SETTINGS_PATH" && -w "$(dirname "$SETTINGS_PATH")" ]]; then
    privilege=()
elif command -v sudo >/dev/null 2>&1 && sudo -n true; then
    privilege=(sudo -n)
else
    printf 'Settings path requires write access or passwordless sudo: %s\n' \
        "$SETTINGS_PATH" >&2
    exit 1
fi
"${privilege[@]}" cp -a "$SETTINGS_PATH" "$backup"
"${privilege[@]}" "$PYTHON" - "$SETTINGS_PATH" <<'PY'
from pathlib import Path
import os
import sys
import yaml

path = Path(sys.argv[1])
value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
if not isinstance(value, dict):
    raise SystemExit("settings must be a YAML object")
lmcache = value.setdefault("lmcache", {})
if not isinstance(lmcache, dict):
    raise SystemExit("lmcache settings must be an object")
lmcache["enabled"] = False
temporary = path.with_suffix(path.suffix + ".new")
with temporary.open("w", encoding="utf-8") as handle:
    yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, path)
PY

systemctl --user stop qwen38-v100-tp2-vllm.service
systemctl --user restart qwen38-v100-tp2-lmcache.service
systemctl --user start qwen38-v100-tp2-vllm.service
printf 'Rollback requested; previous settings saved at %s\n' "$backup"
