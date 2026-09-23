#!/usr/bin/env bash
# Start the three stub test nodes in the background and print their PIDs/logs.
# Stops them all on Ctrl-C / script exit.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SAMPLE="${SAMPLE:-testdata/trusted_sample.json}"
PORT_ALPHA="${PORT_ALPHA:-50061}"
PORT_BETA="${PORT_BETA:-50062}"
PORT_GAMMA="${PORT_GAMMA:-50063}"
mkdir -p data

go build -mod=vendor -o bin/node ./cmd/node

PIDS=()
start() { # id port config
  local id="$1" port="$2" cfg="$3"
  ./bin/node --addr "127.0.0.1:${port}" --sample "$SAMPLE" --config "$cfg" >"data/${id}.log" 2>&1 &
  PIDS+=("$!")
  echo "node ${id} pid=$! on 127.0.0.1:${port}"
}
trap 'echo; echo "stopping nodes..."; kill "${PIDS[@]}" 2>/dev/null || true' EXIT

start alpha "$PORT_ALPHA" testdata/node_alpha.json
start beta  "$PORT_BETA"  testdata/node_beta.json
start gamma "$PORT_GAMMA" testdata/node_gamma.json

echo "all nodes running; Ctrl-C to stop. Logs in data/{alpha,beta,gamma}.log"
wait
