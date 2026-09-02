#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "install-tail-control-tls must run as root" >&2
    exit 1
fi

SCRIPT_DIR="$(
    cd "$(dirname "${BASH_SOURCE[0]}")" &&
        pwd
)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
INSTALL_DIR="/opt/1panel/ai-router/bin"
SYSTEMD_DIR="/etc/systemd/system"
TLS_DIR="/opt/1panel/ai-router-control-tls"
LEGACY_TLS_DIR="/opt/1panel/ai-router/tls"
DOMAIN="ai-x10drg.taild500c8.ts.net"

install -d -m 0750 -o 10001 -g 10001 "${TLS_DIR}"
if [[ -f "${LEGACY_TLS_DIR}/${DOMAIN}.crt" ]]; then
    mv -f \
        "${LEGACY_TLS_DIR}/${DOMAIN}.crt" \
        "${TLS_DIR}/${DOMAIN}.crt"
fi
if [[ -f "${LEGACY_TLS_DIR}/${DOMAIN}.key" ]]; then
    mv -f \
        "${LEGACY_TLS_DIR}/${DOMAIN}.key" \
        "${TLS_DIR}/${DOMAIN}.key"
fi
if [[ -f "${TLS_DIR}/${DOMAIN}.crt" ]]; then
    chown 10001:10001 "${TLS_DIR}/${DOMAIN}.crt"
    chmod 0644 "${TLS_DIR}/${DOMAIN}.crt"
fi
if [[ -f "${TLS_DIR}/${DOMAIN}.key" ]]; then
    chown 10001:10001 "${TLS_DIR}/${DOMAIN}.key"
    chmod 0600 "${TLS_DIR}/${DOMAIN}.key"
fi

install -d -m 0755 "${INSTALL_DIR}"
install -m 0755 \
    "${SCRIPT_DIR}/renew-tail-control-cert.sh" \
    "${INSTALL_DIR}/renew-tail-control-cert"
install -m 0644 \
    "${PROJECT_DIR}/systemd/ai-router-tail-cert-renew.service" \
    "${SYSTEMD_DIR}/ai-router-tail-cert-renew.service"
install -m 0644 \
    "${PROJECT_DIR}/systemd/ai-router-tail-cert-renew.timer" \
    "${SYSTEMD_DIR}/ai-router-tail-cert-renew.timer"

AI_ROUTER_TLS_SKIP_RESTART=1 \
    "${INSTALL_DIR}/renew-tail-control-cert"

systemctl daemon-reload
systemctl enable --now ai-router-tail-cert-renew.timer

echo "AI Router tail control TLS is installed."
echo "Start or recreate router-control-tail with docker compose."
