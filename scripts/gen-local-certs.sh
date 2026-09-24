#!/usr/bin/env bash
# Generate a local CA and a webhook serving certificate for development.
# NOT for production (use cert-manager there).
#
# Output dir defaults to /tmp/k8s-webhook-server/serving-certs, which is the
# path cmd/webhook reads by default.
set -euo pipefail

OUT="${1:-/tmp/k8s-webhook-server/serving-certs}"
DAYS="${DAYS:-365}"
mkdir -p "$OUT"
cd "$OUT"

if [ ! -f ca.crt ]; then
  echo ">> generating CA"
  openssl genrsa -out ca.key 2048 2>/dev/null
  openssl req -x509 -new -nodes -key ca.key -days "$DAYS" \
    -subj "/CN=timer-dev-ca" -out ca.crt 2>/dev/null
fi

echo ">> generating webhook server key/cert (127.0.0.1, localhost, timer-webhook.timer-system.svc)"
openssl genrsa -out tls.key 2048 2>/dev/null
cat > openssl-san.cnf <<EOF
[req]
distinguished_name = dn
req_extensions = v3_req
[dn]
CN = timer-webhook
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = localhost
DNS.2 = timer-webhook
DNS.3 = timer-webhook.timer-system.svc
IP.1  = 127.0.0.1
EOF
openssl req -new -key tls.key -subj "/CN=timer-webhook" \
  -config openssl-san.cnf -out tls.csr 2>/dev/null
openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days "$DAYS" -extensions v3_req -extfile openssl-san.cnf -out tls.crt 2>/dev/null
rm -f tls.csr openssl-san.cnf

echo ">> wrote:"
ls -1 "$OUT"
echo
echo "CA bundle (for webhook clientConfig.caBundle):"
cat ca.crt
