#!/usr/bin/env bash
# Generates the Phase 2 TLS PKI for HiveMQ-MQTT:
#   - Campus root CA (self-signed)
#   - HiveMQ server cert
#   - 100 per-node MQTT client certs
#   - 10 per-gateway client certs
#   - JKS keystores/truststores consumed by HiveMQ config.xml
#
# Requirements:
#   - openssl >= 1.1
#   - keytool (comes with JDK; optional - only needed to build .jks files)
#
# Usage (from repo root):
#     bash infra/certs/gen_certs.sh
#
# Output layout:
#     infra/certs/ca/               root CA cert + key
#     infra/certs/hivemq/           server keystore (.jks) + truststore (.jks)
#     infra/certs/nodes/            per-room PEM bundles
#     infra/certs/gateways/         per-gateway PEM bundles
#
# All passphrases default to `changeit` to match config.xml.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CA_DIR="$HERE/ca"
HIVEMQ_DIR="$HERE/hivemq"
NODES_DIR="$HERE/nodes"
GW_DIR="$HERE/gateways"

mkdir -p "$CA_DIR" "$HIVEMQ_DIR" "$NODES_DIR" "$GW_DIR"

PASS="changeit"
DAYS=3650

# ---------- CA ----------
if [[ ! -f "$CA_DIR/ca.key" ]]; then
    openssl genrsa -out "$CA_DIR/ca.key" 4096
    openssl req -x509 -new -nodes -key "$CA_DIR/ca.key" -sha256 -days $DAYS \
        -out "$CA_DIR/ca.crt" \
        -subj "/C=CA/ST=ON/L=Toronto/O=CampusPhase2/CN=Campus-Phase2-Root-CA"
    echo "Generated Root CA"
fi

gen_cert() {
    local name="$1" cn="$2" outdir="$3"
    openssl genrsa -out "$outdir/$name.key" 2048
    openssl req -new -key "$outdir/$name.key" -out "$outdir/$name.csr" \
        -subj "/C=CA/ST=ON/L=Toronto/O=CampusPhase2/CN=$cn"
    openssl x509 -req -in "$outdir/$name.csr" \
        -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
        -out "$outdir/$name.crt" -days $DAYS -sha256
    rm "$outdir/$name.csr"
}

# ---------- HiveMQ server ----------
if [[ ! -f "$HIVEMQ_DIR/hivemq.crt" ]]; then
    gen_cert "hivemq" "hivemq" "$HIVEMQ_DIR"
    echo "Generated HiveMQ server cert"
fi

# Optional JKS packaging (requires keytool).
if command -v keytool >/dev/null 2>&1; then
    if [[ ! -f "$HIVEMQ_DIR/hivemq-keystore.jks" ]]; then
        openssl pkcs12 -export -inkey "$HIVEMQ_DIR/hivemq.key" \
            -in "$HIVEMQ_DIR/hivemq.crt" -certfile "$CA_DIR/ca.crt" \
            -out "$HIVEMQ_DIR/hivemq.p12" -name hivemq -passout pass:$PASS
        keytool -importkeystore -srckeystore "$HIVEMQ_DIR/hivemq.p12" \
            -srcstoretype PKCS12 -srcstorepass $PASS \
            -destkeystore "$HIVEMQ_DIR/hivemq-keystore.jks" \
            -deststoretype JKS -deststorepass $PASS -noprompt
        keytool -importcert -keystore "$HIVEMQ_DIR/hivemq-truststore.jks" \
            -storepass $PASS -file "$CA_DIR/ca.crt" -alias campus-ca -noprompt
        echo "Built HiveMQ JKS keystores"
    fi
else
    echo "WARN: keytool not found; skipping JKS packaging. Copy PEM bundles into HiveMQ manually."
fi

# ---------- 100 per-node MQTT client certs ----------
for floor in 01 02 03 04 05 06 07 08 09 10; do
    for idx in 01 02 03 04 05 06 07 08 09 10; do
        room_code=$((10#$floor * 100 + 10#$idx))
        name="b01-f${floor}-r${room_code}"
        [[ -f "$NODES_DIR/$name.crt" ]] && continue
        gen_cert "$name" "$name" "$NODES_DIR"
    done
done
echo "Generated 100 MQTT node certificates"

# ---------- 10 gateway certs ----------
for floor in 01 02 03 04 05 06 07 08 09 10; do
    name="gateway-f$floor"
    [[ -f "$GW_DIR/$name.crt" ]] && continue
    gen_cert "$name" "$name" "$GW_DIR"
done
echo "Generated 10 gateway certificates"

echo "TLS PKI ready."
