#!/usr/bin/env bash
# 优雅停止两条 anvil 链（SIGTERM，anvil 会把状态写回 --state 文件）。
set -euo pipefail
cd "$(dirname "$0")/.."

for name in alpha beta; do
  pidfile=".run/${name}.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    pid="$(cat "$pidfile")"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    echo "$name (pid $pid) 已停止，状态保存在 .run/${name}-state.json"
  else
    echo "$name 未在运行"
  fi
  rm -f "$pidfile"
done
