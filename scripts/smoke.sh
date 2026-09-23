#!/usr/bin/env bash
# End-to-end smoke test against a running proxy:
# starts a local HTTP origin, requests it through the SOCKS5 proxy with curl,
# then queries the HTTP control plane.
#
# Usage:
#   ./scripts/smoke.sh            # uses 127.0.0.1:1080 / 127.0.0.1:8080
#   SOCKS5_ADDR=127.0.0.1:11080 HTTP_ADDR=127.0.0.1:18080 ./scripts/smoke.sh
set -euo pipefail

SOCKS5_ADDR="${SOCKS5_ADDR:-127.0.0.1:1080}"
HTTP_ADDR="${HTTP_ADDR:-127.0.0.1:8080}"
PROXY_USER="${PROXY_USER:-alice}"
PROXY_PASS="${PROXY_PASS:-s3cret!}"

cd "$(dirname "$0")/.."

# 1. A throwaway origin server bound to loopback (inside the allowlist).
origin_dir="$(mktemp -d)"
# Pick an ephemeral free port up front.
origin_port="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
trap 'kill "${origin_pid:-0}" 2>/dev/null; rm -rf "$origin_dir"' EXIT
echo 'hello from the local test origin' > "$origin_dir/index.html"
( cd "$origin_dir" && python3 -m http.server "$origin_port" --bind 127.0.0.1 >"$origin_dir/origin.log" 2>&1 ) &
origin_pid=$!
for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$origin_port/" >/dev/null 2>&1 && break
  sleep 0.1
done
echo "origin:  http://127.0.0.1:$origin_port"

# 2. Request through SOCKS5 (IPv4 CONNECT + username/password auth).
echo "--- GET via SOCKS5 (IPv4) ---"
curl -fsS --socks5-hostname "$PROXY_USER:$PROXY_PASS@$SOCKS5_ADDR" \
  "http://127.0.0.1:$origin_port/"

# 3. Same target addressed by the IPv6 loopback.
echo "--- GET via SOCKS5 (IPv6) ---"
curl -fsS -g --socks5-hostname "$PROXY_USER:$PROXY_PASS@$SOCKS5_ADDR" \
  "http://[::1]:$origin_port/" || echo "(IPv6 skipped: origin may listen on IPv4 only)"

# 4. Same target addressed by domain name (ATYP=DOMAIN, "localhost").
echo "--- GET via SOCKS5 (domain localhost) ---"
curl -fsS --socks5-hostname "$PROXY_USER:$PROXY_PASS@$SOCKS5_ADDR" \
  "http://localhost:$origin_port/"

# 5. A destination outside the allowlist must fail.
echo "--- disallowed destination (expected to fail) ---"
if curl -fsS --max-time 5 --socks5-hostname "$PROXY_USER:$PROXY_PASS@$SOCKS5_ADDR" \
     "http://example.com/" >/dev/null 2>&1; then
  echo "ERROR: external destination was not blocked"
  exit 1
else
  echo "correctly rejected (curl exit $?)"
fi

# 6. Wrong credentials must fail.
echo "--- bad credentials (expected to fail) ---"
if curl -fsS --max-time 5 --socks5-hostname "nope:wrong@$SOCKS5_ADDR" \
     "http://127.0.0.1:$origin_port/" >/dev/null 2>&1; then
  echo "ERROR: bad credentials were accepted"
  exit 1
else
  echo "correctly rejected (curl exit $?)"
fi

# 7. Control plane.
echo "--- /healthz ---"; curl -fsS "http://$HTTP_ADDR/healthz"; echo
echo "--- /stats ---";   curl -fsS "http://$HTTP_ADDR/stats"; echo
echo "--- /allowlist ---"; curl -fsS "http://$HTTP_ADDR/allowlist"; echo
echo "SMOKE OK"
