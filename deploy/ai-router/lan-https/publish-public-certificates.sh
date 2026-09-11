#!/bin/sh
set -eu
SOURCE=/opt/1panel/ai-router-lan-https/ip-tls
DEST=/opt/1panel/ai-router/lan-https
install -d -m 0755 "$DEST"
install -m 0644 "$SOURCE/ca.crt" "$DEST/ca.crt"
install -m 0644 "$SOURCE/live/server.crt" "$DEST/server.crt"
python3 - <<'PY'
import json,subprocess
from pathlib import Path
from datetime import datetime,timezone
p=Path("/opt/1panel/ai-router/lan-https/connection.json")
active=subprocess.run(["systemctl","is-active","--quiet","ai-router-lan-tls-reload.timer"]).returncode==0
data={"base_url":"https://192.168.2.66:4000/v1","renewal":{"active":active,"last_check_at":datetime.now(timezone.utc).isoformat(),"check_interval_hours":24,"renew_before_days":30}}
stage=p.with_suffix(".next")
stage.write_text(json.dumps(data))
stage.chmod(0o644)
stage.replace(p)
PY
