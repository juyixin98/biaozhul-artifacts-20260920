#!/usr/bin/env bash
# gen-certs.sh — mint a real (self-signed) CA and a serving certificate for
# the admission webhook using openssl. No cert-manager required.
#
# Outputs (under $OUTDIR, default hack/_output):
#   ca.crt / ca.key           self-signed CA
#   tls.crt / tls.key         server cert for the webhook Service
#   webhook-ca-bundle.txt     base64 caBundle for ValidatingWebhookConfiguration
#   tls-secret.yaml           Secret(quota-system/quota-webhook-tls) manifest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="${OUTDIR:-$SCRIPT_DIR/_output}"
SVC_NAME="quota-webhook-service"
SVC_NS="quota-system"
DAYS="${DAYS:-3650}"

mkdir -p "$OUTDIR"
cd "$OUTDIR"

echo "[gen-certs] writing certs into $OUTDIR"

# 1. Self-signed CA -----------------------------------------------------------
openssl genrsa -out ca.key 4096 2>/dev/null
openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
    -subj "/CN=quota-webhook-ca" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" \
    -out ca.crt

# 2. Server key + CSR ---------------------------------------------------------
openssl genrsa -out tls.key 2048 2>/dev/null
cat > openssl-san.cnf <<EOF
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no
[dn]
CN = ${SVC_NAME}.${SVC_NS}.svc
[v3_req]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${SVC_NAME}
DNS.2 = ${SVC_NAME}.${SVC_NS}
DNS.3 = ${SVC_NAME}.${SVC_NS}.svc
DNS.4 = ${SVC_NAME}.${SVC_NS}.svc.cluster.local
EOF
openssl req -new -key tls.key -out tls.csr -config openssl-san.cnf

# 3. Sign with the CA ----------------------------------------------------------
openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out tls.crt -days "$DAYS" -sha256 \
    -extensions v3_req -extfile openssl-san.cnf

# 4. caBundle + TLS Secret manifest -------------------------------------------
# A single base64 blob, no line wrapping.
base64 -w0 ca.crt > webhook-ca-bundle.txt

TLS_CRT_B64="$(base64 -w0 tls.crt)"
TLS_KEY_B64="$(base64 -w0 tls.key)"
cat > tls-secret.yaml <<EOF
---
apiVersion: v1
kind: Secret
metadata:
  name: quota-webhook-tls
  namespace: ${SVC_NS}
type: kubernetes.io/tls
data:
  tls.crt: ${TLS_CRT_B64}
  tls.key: ${TLS_KEY_B64}
EOF

# 5. Verify the chain with openssl (real cryptographic verification) ----------
if openssl verify -CAfile ca.crt tls.crt >/dev/null; then
    echo "[gen-certs] openssl verify: OK (tls.crt chains to generated CA)"
else
    echo "[gen-certs] openssl verify: FAILED" >&2
    exit 1
fi

echo "[gen-certs] done. caBundle bytes: $(wc -c < webhook-ca-bundle.txt)"
