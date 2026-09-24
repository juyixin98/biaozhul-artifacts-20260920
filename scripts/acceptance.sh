#!/usr/bin/env bash
# End-to-end acceptance for the Modbus write-receipt service.
#
# 1. builds both binaries
# 2. starts a real Modbus TCP server on 127.0.0.1:1502 with a fresh SQLite DB
# 3. exercises every examples/*.json scenario (exceptions, sticky packets,
#    byte-level fragmentation, duplicate transaction ids, mid-frame disconnects)
# 4. runs direct CLI read/write commands
# 5. verifies the cryptographic receipt chain (SHA-256 links + HMAC-SHA256)
# 6. demonstrates tamper detection, then stops the server
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ADDR="127.0.0.1:1502"
WORK="$(mktemp -d)"
DB="$WORK/receipts.db"
KEY="$WORK/hmac.key"
SERVER_LOG="$WORK/server.log"
printf 'acceptance-test-hmac-key' > "$KEY"

SERVER_BIN="$ROOT/bin/modbus-server"
CLIENT_BIN="$ROOT/bin/modbus-replay"

echo "== build =="
go build -o "$SERVER_BIN" ./cmd/modbus-server
go build -o "$CLIENT_BIN"  ./cmd/modbus-replay

echo "== start server on $ADDR (db=$DB) =="
"$SERVER_BIN" -listen "$ADDR" -db "$DB" -registers 100 -key-file "$KEY" >"$SERVER_LOG" 2>&1 &
SRV_PID=$!
trap 'kill "$SRV_PID" >/dev/null 2>&1 || true' EXIT

# Wait for the listener (max ~5s).
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/1502) 2>/dev/null; then exec 3>&- 3<&-; break; fi
  sleep 0.1
done

pass=0
fail=0
run_scenario() {
  local f="$1"
  echo
  echo "== scenario: $(basename "$f") =="
  if "$CLIENT_BIN" run -script "$f"; then
    pass=$((pass+1))
  else
    fail=$((fail+1))
    echo "FAILED scenario: $f"
  fi
}

for f in examples/*.json; do
  run_scenario "$f"
done

echo
echo "== direct CLI commands =="
"$CLIENT_BIN" write -addr "$ADDR" -unit 1 -txn 200 -at 40 -values 0x0A0B,0x0C0D,1000
"$CLIENT_BIN" read  -addr "$ADDR" -unit 1 -txn 201 -at 39 -qty 5
echo "-- fragmented CLI write (1 byte/segment, 2ms gap) --"
"$CLIENT_BIN" frag  -addr "$ADDR" -unit 1 -txn 202 -at 50 -values 1,2,3,4 -frag-size 1 -gap 2ms
"$CLIENT_BIN" read  -addr "$ADDR" -unit 1 -txn 203 -at 50 -qty 4

echo
echo "== exception directly from the CLI (expected exit code 3) =="
set +e
"$CLIENT_BIN" read -addr "$ADDR" -unit 1 -txn 204 -at 500 -qty 1
rc=$?
set -e
if [ "$rc" -ne 3 ]; then
  echo "FAIL: expected CLI exit code 3 for exception, got $rc"; fail=$((fail+1))
else
  echo "OK: CLI reported Modbus exception with exit code 3"; pass=$((pass+1))
fi

echo
echo "== receipts and audit log =="
"$CLIENT_BIN" list -db "$DB" -table receipts -n 5 || true
"$CLIENT_BIN" list -db "$DB" -table exceptions -n 8 || true

echo
echo "== verify cryptographic receipt chain =="
"$CLIENT_BIN" verify -db "$DB" -key-file "$KEY"
pass=$((pass+1))

echo
echo "== negative: verify must fail with the wrong key =="
set +e
printf 'wrong-key' > "$WORK/wrong.key"
"$CLIENT_BIN" verify -db "$DB" -key-file "$WORK/wrong.key"
rc=$?
set -e
if [ "$rc" -ne 4 ]; then
  echo "FAIL: expected exit 4 with wrong key, got $rc"; fail=$((fail+1))
else
  echo "OK: wrong key rejected (exit 4)"; pass=$((pass+1))
fi

echo
echo "== negative: tamper with a receipt row and re-verify =="
# sqlite3 CLI can copy a live WAL database with .backup; otherwise a
# small Go helper performs the same copy + corruption through SQL.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$WORK/tampered.db'"
  sqlite3 "$WORK/tampered.db" "UPDATE receipts SET values_hex='ffff0000' WHERE seq=1;"
else
  cat >"$WORK/tamper.go" <<'EOF'
package main

import (
	"database/sql"
	"fmt"
	"os"

	_ "modernc.org/sqlite"
)

func main() {
	if len(os.Args) != 3 {
		panic("usage: tamper SRC DST")
	}
	// Open source read-only-ish and create a destination by replaying the
	// receipts table through SQL (schema + rows), then corrupt seq=1.
	src, err := sql.Open("sqlite", "file:"+os.Args[1]+"?mode=ro")
	must(err)
	defer src.Close()

	dst, err := sql.Open("sqlite", os.Args[2])
	must(err)
	defer dst.Close()
	for _, stmt := range []string{
		`PRAGMA journal_mode=DELETE`,
		`CREATE TABLE receipts (
		    seq INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id INTEGER NOT NULL,
		    unit_id INTEGER NOT NULL, address INTEGER NOT NULL, quantity INTEGER NOT NULL,
		    values_hex TEXT NOT NULL, body_sha256 TEXT NOT NULL, prev_sha256 TEXT NOT NULL,
		    chain_sha256 TEXT NOT NULL UNIQUE, hmac_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)`,
	} {
		_, err := dst.Exec(stmt)
		must(err)
	}
	rows, err := src.Query(`SELECT seq, transaction_id, unit_id, address, quantity,
	    values_hex, body_sha256, prev_sha256, chain_sha256, hmac_sha256, created_at FROM receipts ORDER BY seq`)
	must(err)
	defer rows.Close()
	for rows.Next() {
		var seq, txn, unit, addr, qty int
		var vh, body, prev, chain, hmac, ts string
		must(rows.Scan(&seq, &txn, &unit, &addr, &qty, &vh, &body, &prev, &chain, &hmac, &ts))
		_, err := dst.Exec(`INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
			seq, txn, unit, addr, qty, vh, body, prev, chain, hmac, ts)
		must(err)
	}
	_, err = dst.Exec(`UPDATE receipts SET values_hex='ffff0000' WHERE seq=1`)
	must(err)
	fmt.Println("copied + tampered seq=1 via SQL")
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
EOF
  (cd "$ROOT" && go run "$WORK/tamper.go" "$DB" "$WORK/tampered.db")
fi
set +e
"$CLIENT_BIN" verify -db "$WORK/tampered.db" -key-file "$KEY"
rc=$?
set -e
if [ "$rc" -ne 4 ]; then
  echo "FAIL: tampered DB accepted (rc=$rc)"; fail=$((fail+1))
else
  echo "OK: tampered receipt chain detected (exit 4)"; pass=$((pass+1))
fi

echo
echo "=============================================="
echo "acceptance summary: $pass passed, $fail failed"
echo "server log tail:"
tail -n 12 "$SERVER_LOG" || true
echo "artifacts kept in: $WORK"
echo "=============================================="
[ "$fail" -eq 0 ]
