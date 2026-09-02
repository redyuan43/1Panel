#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "install-training-archive must run as root" >&2
    exit 1
fi

TRAINING_DIR="/opt/1panel/ai-router-training"
KEY_FILE="${TRAINING_DIR}/training.key"
DATABASE_FILE="${TRAINING_DIR}/conversations.sqlite3"
EXPORT_DIR="${TRAINING_DIR}/exports"

install -d -m 0700 -o 10001 -g 10001 "${TRAINING_DIR}"
install -d -m 0700 -o 10001 -g 10001 "${EXPORT_DIR}"

if [[ ! -f "${KEY_FILE}" ]]; then
    temporary="${KEY_FILE}.next"
    python3 -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
        >"${temporary}"
    install -m 0600 -o 10001 -g 10001 \
        "${temporary}" \
        "${KEY_FILE}"
    unlink "${temporary}"
fi

python3 - "${KEY_FILE}" <<'PY'
import sys
from pathlib import Path

from cryptography.fernet import Fernet

Fernet(Path(sys.argv[1]).read_bytes().strip())
PY

chown 10001:10001 "${KEY_FILE}"
chmod 0600 "${KEY_FILE}"
if [[ -f "${DATABASE_FILE}" ]]; then
    chown 10001:10001 "${DATABASE_FILE}"
    chmod 0600 "${DATABASE_FILE}"
fi

echo "Encrypted AI Router training archive is prepared."
