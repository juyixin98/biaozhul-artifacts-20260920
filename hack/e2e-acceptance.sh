#!/usr/bin/env bash
# Real end-to-end acceptance against a kind cluster.
#
# Creates a dedicated Certificate + CA, then verifies with crypto/x509 via
# `openssl verify` and kubectl read-backs:
#   1. chain + hostname verification against the test CA
#   2. tls.crt / tls.key public-key match
#   3. Secret annotations == status serial/thumbprint
#   4. near-expiry auto-renewal rotates the serial and the new chain re-verifies
#   5. spec domain change produces a cert carrying the new SAN
#   6. issuance failure (CA removed) never creates/deletes the serving Secret
#
# Exits non-zero on the first failed assertion. Pure backend; no UI.
set -euo pipefail

NS="${NS:-default}"
CA="${CA:-acc-ca}"
CERT="${CERT:-acc-cert}"
SECRET="${SECRET:-acc-cert-tls}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass() { printf '\033[32m[PASS]\033[0m %s\n' "$1"; }
info() { printf '\033[36m[ .. ]\033[0m %s\n' "$1"; }
fail() { printf '\033[31m[FAIL]\033[0m %s\n' "$1"; exit 1; }

command -v kubectl >/dev/null || fail "kubectl required"
command -v openssl >/dev/null || fail "openssl required"

k() { kubectl -n "$NS" "$@"; }

extract() { # name key outfile  (dots in key are escaped for jsonpath)
  local escaped; escaped=$(printf '%s' "$2" | sed 's/\./\\./g')
  k get secret "$1" -o jsonpath="{.data.${escaped}}" | base64 -d > "$3"
}

info "generating a fresh sample CA Secret ($CA)"
go run ./cmd/gentestca -namespace "$NS" -name "$CA" -lifetime 48h 2>"$WORK/ca.err" | k apply -f -
grep -q "BEGIN CERTIFICATE" <(k get secret "$CA" -o jsonpath='{.data.tls\.crt}' | base64 -d) \
  || fail "CA secret missing tls.crt"
pass "CA Secret present"

info "creating Certificate $CERT (20m lifetime, 14m renewBefore -> ~1m healthy window)"
cat <<YAML | k apply -f -
apiVersion: certificates.example.com/v1alpha1
kind: Certificate
metadata:
  name: $CERT
spec:
  dnsNames: ["acc.app.local", "www.acc.app.local"]
  secretName: $SECRET
  duration: 20m
  renewBefore: 14m
  issuer:
    name: $CA
YAML

# Wait for Ready=True
for i in $(seq 1 24); do
  ready=$(k get certificate "$CERT" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  [ "$ready" = "True" ] && break
  sleep 2
done
[ "${ready:-}" = "True" ] || { k describe certificate "$CERT"; fail "certificate never became Ready"; }
pass "Certificate Ready=True"

extract "$SECRET" tls.crt "$WORK/leaf.crt"
extract "$SECRET" tls.key "$WORK/leaf.key"
extract "$SECRET" ca.crt  "$WORK/ca.crt"

# 1. Real chain + hostname verification.
for host in acc.app.local www.acc.app.local; do
  openssl verify -CAfile "$WORK/ca.crt" -verify_hostname "$host" "$WORK/leaf.crt" | grep -q ": OK" \
    || fail "chain/hostname verification failed for $host"
done
pass "x509 chain verifies against CA for both SANs"

# 2. cert/key match.
diff <(openssl x509 -in "$WORK/leaf.crt" -noout -pubkey) \
     <(openssl pkey -in "$WORK/leaf.key" -pubout 2>/dev/null) >/dev/null \
  || fail "tls.crt public key does not match tls.key"
pass "tls.crt and tls.key are a matching pair"

# 3. Secret annotation serial/thumbprint == status.
cert_serial=$(openssl x509 -in "$WORK/leaf.crt" -noout -serial | cut -d= -f2 | sed 's/\(..\)/\1:/g;s/:$//')
status_serial=$(k get certificate "$CERT" -o jsonpath='{.status.serial}')
ann_serial=$(k get secret "$SECRET" -o jsonpath='{.metadata.annotations.certificates\.example\.com/serial}')
[ "$cert_serial" = "$status_serial" ] || fail "status serial $status_serial != cert $cert_serial"
[ "$cert_serial" = "$ann_serial" ]   || fail "secret annotation serial $ann_serial != cert $cert_serial"
cert_thumb=$(openssl x509 -in "$WORK/leaf.crt" -noout -fingerprint -sha256 | cut -d= -f2)
status_thumb=$(k get certificate "$CERT" -o jsonpath='{.status.thumbprint}')
[ "$cert_thumb" = "$status_thumb" ] || fail "status thumbprint mismatch"
pass "Secret tls.crt, serial annotation and status summary are identical"

# 4. Near-expiry renewal (healthy window ~1m; allow up to 100s).
first_serial="$status_serial"
info "waiting for the real renewal window (serial must rotate)..."
new_serial=""
for i in $(seq 1 20); do
  sleep 5
  new_serial=$(k get certificate "$CERT" -o jsonpath='{.status.serial}')
  [ "$new_serial" != "$first_serial" ] && break
done
[ "$new_serial" != "$first_serial" ] || fail "certificate did not renew within the window"
pass "certificate renewed at the bound window ($first_serial -> $new_serial)"

extract "$SECRET" tls.crt "$WORK/leaf2.crt"
extract "$SECRET" tls.key "$WORK/leaf2.key"
openssl verify -CAfile "$WORK/ca.crt" -verify_hostname acc.app.local "$WORK/leaf2.crt" | grep -q ": OK" \
  || fail "renewed chain failed verification"
diff <(openssl x509 -in "$WORK/leaf2.crt" -noout -pubkey) \
     <(openssl pkey -in "$WORK/leaf2.key" -pubout 2>/dev/null) >/dev/null \
  || fail "renewed cert/key mismatch"
k get secret "${SECRET}-pending" >/dev/null 2>&1 && fail "pending secret leaked after renewal" || true
pass "renewed chain verifies, key matches, pending secret cleaned up"

# 5. Spec domain change.
info "changing dnsNames (adding api.acc.app.local)..."
k patch certificate "$CERT" --type merge -p '{"spec":{"dnsNames":["acc.app.local","www.acc.app.local","api.acc.app.local"]}}' >/dev/null
for i in $(seq 1 20); do
  sleep 2
  extract "$SECRET" tls.crt "$WORK/leaf3.crt" 2>/dev/null || continue
  if openssl x509 -in "$WORK/leaf3.crt" -noout -ext subjectAltName | grep -q "api.acc.app.local"; then break; fi
done
openssl x509 -in "$WORK/leaf3.crt" -noout -ext subjectAltName | grep -q "api.acc.app.local" \
  || fail "new SAN not present after spec change"
openssl verify -CAfile "$WORK/ca.crt" -verify_hostname api.acc.app.local "$WORK/leaf3.crt" | grep -q ": OK" \
  || fail "new-SAN chain verification failed"
pass "spec change produced a valid certificate for the new domain"

# 6. Issuance failure never touches the serving secret.
info "simulating CA outage and creating a second cert..."
k get secret "$CA" -o yaml | sed "s/name: $CA/name: ${CA}-bak/; /resourceVersion:/d; /uid:/d; /creationTimestamp:/d" | k apply -f - >/dev/null
k delete secret "$CA" >/dev/null
cat <<YAML | k apply -f -
apiVersion: certificates.example.com/v1alpha1
kind: Certificate
metadata:
  name: ${CERT}-outage
spec:
  dnsNames: ["outage.acc.app.local"]
  secretName: ${SECRET}-outage
  duration: 20m
  renewBefore: 14m
  issuer:
    name: $CA
YAML
sleep 6
k get secret "${SECRET}-outage" >/dev/null 2>&1 && fail "secret must NOT be created while CA unavailable"
[ "$(k get certificate "$CERT" -o jsonpath='{.status.serial}')" = "$(openssl x509 -in "$WORK/leaf3.crt" -noout -serial | cut -d= -f2 | sed 's/\(..\)/\1:/g;s/:$//')" ] \
  || fail "existing serving certificate changed during CA outage"
pass "issuance failure retained the existing secret and created no half-written secret"

# Restore so the environment is clean.
k get secret "${CA}-bak" -o yaml | sed "s/name: ${CA}-bak/name: $CA/; /resourceVersion:/d; /uid:/d; /creationTimestamp:/d" | k apply -f - >/dev/null
k delete secret "${CA}-bak" >/dev/null

printf '\n\033[32m=== ALL ACCEPTANCE CHECKS PASSED ===\033[0m\n'
