#!/usr/bin/env bash
# Send every samples/*.http raw request file to the local reference
# service and print the status line + X-Frame-* headers of each reply.
#
# Usage:
#   scripts/send_samples.sh [HOST:PORT] [--bytewise]
#
# Requires python3 (used only as a TCP client with exact byte control;
# the project itself has zero dependencies).
set -u

ADDR="127.0.0.1:8080"
MODE="bulk"
for arg in "$@"; do
  case "$arg" in
    --bytewise) MODE="bytewise" ;;
    --help|-h)
      sed -n '2,9p' "$0"; exit 0 ;;
    *) ADDR="$arg" ;;
  esac
done

HOST="${ADDR%:*}"
PORT="${ADDR##*:}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HERE/../samples"

shopt -s nullglob
for f in "$DIR"/*.http; do
  name="$(basename "$f")"
  echo "=================================================================="
  echo "sample: $name   (delivery: $MODE)"
  HOST="$HOST" PORT="$PORT" MODE="$MODE" FILE="$f" python3 - << 'PYEOF'
import os, socket, time

host = os.environ["HOST"]
port = int(os.environ["PORT"])
mode = os.environ["MODE"]
with open(os.environ["FILE"], "rb") as fh:
    data = fh.read()

s = socket.create_connection((host, port), timeout=5)
if mode == "bytewise":
    # Force the server's incremental path: one TCP segment per byte.
    for b in data:
        s.sendall(bytes([b]))
        time.sleep(0.001)
else:
    s.sendall(data)
s.shutdown(socket.SHUT_WR)

chunks = []
while True:
    try:
        part = s.recv(4096)
    except socket.timeout:
        break
    if not part:
        break
    chunks.append(part)
reply = b"".join(chunks).decode("latin1")

head, _, body = reply.partition("\r\n\r\n")
for line in head.split("\r\n"):
    if (line.startswith("HTTP/")
            or line.lower().startswith("x-framing")
            or line.lower().startswith("x-frame-error")):
        print("  " + line)
flat = body.replace("\n", " ")
print("  body: " + flat[:200] + ("..." if len(flat) > 200 else ""))
s.close()
PYEOF
done
