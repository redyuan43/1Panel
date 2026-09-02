#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${AI_ROUTER_TLS_DOMAIN:-ai-x10drg.taild500c8.ts.net}"
CERT_DIR="${AI_ROUTER_TLS_CERT_DIR:-/opt/1panel/ai-router-control-tls}"
CERT_FILE="${CERT_DIR}/${DOMAIN}.crt"
KEY_FILE="${CERT_DIR}/${DOMAIN}.key"
COMPOSE_FILE="${AI_ROUTER_COMPOSE_FILE:-/home/ai/github/1Panel/deploy/ai-router/compose.yaml}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "renew-tail-control-cert must run as root" >&2
    exit 1
fi

TMP_DIR="$(mktemp -d /tmp/ai-router-tail-cert.XXXXXX)"

cleanup() {
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

certificate_fingerprint() {
    openssl x509 -in "$1" -outform DER |
        sha256sum |
        cut -d' ' -f1
}

certificate_public_key_fingerprint() {
    openssl x509 -in "$1" -pubkey -noout 2>/dev/null |
        openssl pkey -pubin -outform DER 2>/dev/null |
        sha256sum |
        cut -d' ' -f1
}

private_key_fingerprint() {
    openssl pkey -in "$1" -pubout -outform DER 2>/dev/null |
        sha256sum |
        cut -d' ' -f1
}

tailscale cert \
    --min-validity=960h \
    --cert-file="${TMP_DIR}/${DOMAIN}.crt" \
    --key-file="${TMP_DIR}/${DOMAIN}.key" \
    "${DOMAIN}"

openssl x509 \
    -in "${TMP_DIR}/${DOMAIN}.crt" \
    -checkhost "${DOMAIN}" \
    -checkend 2592000 \
    -noout

new_fingerprint="$(certificate_fingerprint "${TMP_DIR}/${DOMAIN}.crt")"
new_public_key="$(
    certificate_public_key_fingerprint "${TMP_DIR}/${DOMAIN}.crt"
)"
current_is_complete=false
if [[ -f "${CERT_FILE}" && -f "${KEY_FILE}" ]]; then
    old_fingerprint="$(certificate_fingerprint "${CERT_FILE}" || true)"
    old_certificate_key="$(
        certificate_public_key_fingerprint "${CERT_FILE}" || true
    )"
    old_private_key="$(private_key_fingerprint "${KEY_FILE}" || true)"
    cert_state="$(stat -c '%a:%u:%g' "${CERT_FILE}" || true)"
    key_state="$(stat -c '%a:%u:%g' "${KEY_FILE}" || true)"
    if [[
        "${new_fingerprint}" == "${old_fingerprint}"
        && "${old_certificate_key}" == "${old_private_key}"
        && "${old_certificate_key}" == "${new_public_key}"
        && "${cert_state}" == "644:10001:10001"
        && "${key_state}" == "600:10001:10001"
    ]]; then
        current_is_complete=true
    fi
fi

if [[ "${current_is_complete}" == "true" ]]; then
    exit 0
fi

install -d -m 0750 -o 10001 -g 10001 "${CERT_DIR}"
install -m 0644 -o 10001 -g 10001 \
    "${TMP_DIR}/${DOMAIN}.crt" \
    "${CERT_FILE}.next"
install -m 0600 -o 10001 -g 10001 \
    "${TMP_DIR}/${DOMAIN}.key" \
    "${KEY_FILE}.next"
mv -f "${CERT_FILE}.next" "${CERT_FILE}"
mv -f "${KEY_FILE}.next" "${KEY_FILE}"

installed_certificate_key="$(
    certificate_public_key_fingerprint "${CERT_FILE}"
)"
installed_private_key="$(private_key_fingerprint "${KEY_FILE}")"
if [[
    "${installed_certificate_key}" != "${installed_private_key}"
    || "${installed_certificate_key}" != "${new_public_key}"
]]; then
    echo "installed certificate and private key do not match" >&2
    exit 1
fi

if [[ "${AI_ROUTER_TLS_SKIP_RESTART:-0}" == "1" ]]; then
    exit 0
fi

container_id="$(
    docker compose -f "${COMPOSE_FILE}" ps -q router-control-tail 2>/dev/null ||
        true
)"
if [[ -z "${container_id}" ]]; then
    exit 0
fi
if [[ "$(docker inspect -f '{{.State.Running}}' "${container_id}")" != "true" ]]; then
    exit 0
fi

docker compose -f "${COMPOSE_FILE}" restart router-control-tail

for _ in $(seq 1 30); do
    if curl --fail --silent --show-error \
        --noproxy "*" \
        "https://${DOMAIN}:4001/health" >/dev/null; then
        exit 0
    fi
    sleep 1
done

echo "tail control HTTPS health check failed" >&2
exit 1
