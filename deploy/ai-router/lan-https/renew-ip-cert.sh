#!/bin/sh
set -eu

CERT_DIR=/opt/1panel/ai-router-lan-https/ip-tls
if openssl x509 -in "${CERT_DIR}/live/server.crt" -checkend 2592000 -noout; then
    exit 0
fi

umask 077
openssl x509 -req -in "${CERT_DIR}/server.csr" \
    -CA "${CERT_DIR}/ca.crt" -CAkey "${CERT_DIR}/ca.key" \
    -CAserial "${CERT_DIR}/ca.srl" -days 365 -sha256 \
    -extfile "${CERT_DIR}/server.ext" -out "${CERT_DIR}/live/server.crt.next"
openssl verify -CAfile "${CERT_DIR}/ca.crt" -verify_ip 192.168.2.66 \
    "${CERT_DIR}/live/server.crt.next"
mv "${CERT_DIR}/live/server.crt.next" "${CERT_DIR}/live/server.crt"
