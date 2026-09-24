#!/usr/bin/env bash
# 启动两条本地测试链：Alpha(默认 8645, chain 31337) / Beta(默认 8646, chain 31338)
# 状态落盘到 .run/{alpha,beta}-state.json，停止后可恢复。
# 端口可用环境变量覆盖：ALPHA_PORT=9000 BETA_PORT=9001 bash scripts/anvil-start.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .run logs

FOUNDRY_BIN="${FOUNDRY_BIN:-$HOME/.foundry/bin}"
ANVIL="$FOUNDRY_BIN/anvil"
ALPHA_PORT="${ALPHA_PORT:-8645}"
BETA_PORT="${BETA_PORT:-8646}"

port_listener_pid() {
  # 返回监听该端口的进程 pid（没有则空）
  ss -ltnp 2>/dev/null | grep "127.0.0.1:$1 " \
    | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1
}

rpc_chain_id() {
  curl -s -m 2 -X POST "http://127.0.0.1:$1" \
    -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
    | sed -n 's/.*"result":"0x\([0-9a-fA-F]*\)".*/\1/p' | head -1
}

start() {
  local name="$1" port="$2" chain_id_dec="$3"
  local pidfile=".run/${name}.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name 已在运行 (pid $(cat "$pidfile"))，先执行 scripts/anvil-stop.sh"
    return
  fi
  local foreign
  foreign="$(port_listener_pid "$port" || true)"
  if [[ -n "$foreign" ]]; then
    echo "端口 $port 已被另一个进程占用 (pid $foreign)。" >&2
    echo "换端口启动：ALPHA_PORT=.. BETA_PORT=.. bash scripts/anvil-start.sh，并设置 HTLC_ALPHA_RPC/HTLC_BETA_RPC" >&2
    exit 1
  fi

  # setsid 让 anvil 成为新会话首进程，脱离本脚本调用方的进程组，
  # 避免上层在命令结束时清理子进程导致链被杀；--state 让退出时落盘。
  setsid nohup "$ANVIL" \
    --silent \
    --host 127.0.0.1 --port "$port" \
    --chain-id "$chain_id_dec" \
    --state ".run/${name}-state.json" \
    > "logs/${name}.log" 2>&1 < /dev/null &
  local pid=$!
  echo $! > "$pidfile"
  sleep 0.5
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name 启动失败，见 logs/${name}.log" >&2
    rm -f "$pidfile"
    exit 1
  fi
  # 确认监听者就是我们自己，且 chainId 符合预期（十六进制）
  local want_hex
  want_hex="$(printf '%x' "$chain_id_dec")"
  local listener got_chain
  for _ in $(seq 1 50); do
    listener="$(port_listener_pid "$port" || true)"
    got_chain="$(rpc_chain_id "$port" || true)"
    [[ "$listener" == "$pid" && "$got_chain" == "$want_hex" ]] && break
    sleep 0.2
  done
  if [[ "$listener" != "$pid" ]]; then
    echo "$name 未能绑定端口 $port（实际监听 pid=${listener:-无}）" >&2
    exit 1
  fi
  echo "$name 启动: pid $pid, rpc http://127.0.0.1:${port}, chain ${chain_id_dec}"
}

start alpha "$ALPHA_PORT" 31337
start beta  "$BETA_PORT" 31338

# 记录实际 RPC，供其它工具 source
cat > .run/chain-env.sh <<EOF
export HTLC_ALPHA_RPC=http://127.0.0.1:${ALPHA_PORT}
export HTLC_BETA_RPC=http://127.0.0.1:${BETA_PORT}
EOF
echo "两条链已就绪（环境信息写入 .run/chain-env.sh）。停止: bash scripts/anvil-stop.sh"
