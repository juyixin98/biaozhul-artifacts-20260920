#!/usr/bin/env bash
#
# acceptance.sh —— 可复现归档打包服务的端到端验收脚本。
#
# 本脚本仅执行本仓库显式定义的操作：构建并启动本地 Go 服务、用夹具目录
# 生成两份 mtime 不同的目录树、通过 JSON 接口打包、比较制品字节与摘要、
# 校验 tar 内容，并验证逃逸符号链接被拒绝。全程只访问 127.0.0.1，
# 不连接任何云平台。
#
# 用法: scripts/acceptance.sh
# 输出: 控制台彩色结果，并把完整日志写入 reports/acceptance-<时间戳>.log

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_DIR="$ROOT/reports"
LOG="$REPORT_DIR/acceptance-$RUN_ID.log"
mkdir -p "$REPORT_DIR"

WORK_BASE="$ROOT/.local/acceptance-$RUN_ID"
WORK_DIR="$WORK_BASE/work"
CACHE_DIR="$WORK_BASE/cache"
FIXTURE_DIR="$WORK_BASE/fixtures"
ARTIFACT_DIR="$WORK_BASE/artifacts"
RESP_DIR="$WORK_BASE/responses"
BIN="$WORK_BASE/reproducible-archive"
mkdir -p "$WORK_DIR" "$CACHE_DIR" "$FIXTURE_DIR" "$ARTIFACT_DIR" "$RESP_DIR"

PASS_COUNT=0
FAIL_COUNT=0

# ---------------------------------------------------------------- 基础工具

log() { printf '%s\n' "$*"; }
section() { printf '\n========== %s ==========\n' "$*"; }

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '  [PASS] %s\n' "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '  [FAIL] %s\n' "$*"; }

# assert_eq <描述> <预期> <实际>
assert_eq() {
  if [[ "$2" == "$3" ]]; then pass "$1 (= $2)"; else fail "$1 (预期=$2 实际=$3)"; fi
}

# assert_cmd <描述> <命令...>：断言命令退出码为 0。
assert_cmd() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc ($*)"; fi
}

# ---------------------------------------------------------------- 夹具构造

# create_tree <根目录> <基准 mtime(unix秒)>
# 覆盖：Unicode 路径、空目录、长文件名（>100 字节，触发 PAX 扩展头）、
# 深层目录、同内容不同权限文件、悬空/合法符号链接、磁盘上的可执行文件。
create_tree() {
  local root="$1" base="$2"
  mkdir -p "$root/空目录-测试" "$root/d e é p/子目录" "$root/nested"

  printf 'alpha\n' > "$root/a.txt";                 chmod 600 "$root/a.txt"
  printf 'alpha\n' > "$root/z.txt";                 chmod 644 "$root/z.txt"
  printf 'beta\n'  > "$root/d e é p/b-ünïcode-文件.txt"; chmod 644 "$root/d e é p/b-ünïcode-文件.txt"
  printf 'gamma\n' > "$root/d e é p/子目录/γ.txt";   chmod 600 "$root/d e é p/子目录/γ.txt"
  printf '#!/bin/sh\necho hi\n' > "$root/脚本.sh";   chmod 755 "$root/脚本.sh"

  # 归档内路径约 220 字节（单名段 < 255 文件系统上限），超出 USTAR 100 字节限制。
  local long
  long="long-$(printf 'あ%.0s' $(seq 1 70)).txt"
  printf 'long-content\n' > "$root/nested/$long"; chmod 644 "$root/nested/$long"

  ln -s a.txt "$root/link-to-a"
  ln -s "d e é p" "$root/link-to-dir"
  ln -s "missing/未来文件" "$root/dangling-link"

  # 给所有条目（含目录）设置以 base 为基准、但彼此交错的 mtime。
  local i=0
  while IFS= read -r -d '' p; do
    touch -h -d "@$((base + i * 37))" "$p" 2>/dev/null || touch -d "@$((base + i * 37))" "$p"
    i=$((i + 1))
  done < <(find "$root" -mindepth 1 -print0)
  touch -d "@$base" "$root"
}

# ---------------------------------------------------------------- 清理 & 服务

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE_URL="http://127.0.0.1:$PORT"
SERVER_LOG="$WORK_BASE/server.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# post_build <请求JSON文件> <响应保存路径> -> 打印 HTTP 状态码
post_build() {
  curl -sS -o "$2" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -X POST "$BASE_URL/v1/builds" --data-binary @"$1"
}

# ================================================================= 主流程
{
section "0. 环境信息"
log "仓库根目录: $ROOT"
log "运行目录:   $WORK_BASE"
log "监听端口:   $PORT"
go version
tar --version | head -1

section "1. 编译与自动化测试（含“遍历顺序反转”确定性测试）"
go vet ./...
pass "go vet 通过"
go test ./...
pass "go test ./... 全部通过"
go build -o "$BIN" ./cmd/server
pass "服务编译成功"

section "2. 启动本地服务（工作目录与缓存目录分离）"
"$BIN" -addr "127.0.0.1:$PORT" -work "$WORK_DIR" -cache "$CACHE_DIR" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if curl -sS "$BASE_URL/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
HEALTH="$(curl -sS "$BASE_URL/healthz" | jq -c .)"
assert_eq "健康探针" '{"status":"ok"}' "$HEALTH"

section "3. 构造两份 mtime 不同、内容相同的目录树"
T1=1000000000   # 2001-09-09T01:46:40Z
T2=1700000000   # 2023-11-14T22:13:20Z
create_tree "$FIXTURE_DIR/tree1" "$T1"
create_tree "$FIXTURE_DIR/tree2" "$T2"
# 再次整体覆盖顶层 mtime，确保两棵树差异明显。
find "$FIXTURE_DIR/tree1" -exec touch -h -d "@$T1" {} + 2>/dev/null || true
find "$FIXTURE_DIR/tree2" -exec touch -h -d "@$T2" {} + 2>/dev/null || true
M1="$(find "$FIXTURE_DIR/tree1/a.txt" -printf '%T@\n')"
M2="$(find "$FIXTURE_DIR/tree2/a.txt" -printf '%T@\n')"
log "tree1/a.txt mtime=$M1"
log "tree2/a.txt mtime=$M2"
if [[ "$M1" != "$M2" ]]; then pass "两棵树的源 mtime 确实不同"; else fail "源 mtime 未区分开"; fi

section "4. 通过 JSON 接口分别打包"
cat > "$RESP_DIR/req1.json" <<JSON
{"source_dir":"$FIXTURE_DIR/tree1","output_path":"$ARTIFACT_DIR/tree1.tar"}
JSON
cat > "$RESP_DIR/req2.json" <<JSON
{"source_dir":"$FIXTURE_DIR/tree2","output_path":"$ARTIFACT_DIR/tree2.tar"}
JSON

CODE1="$(post_build "$RESP_DIR/req1.json" "$RESP_DIR/resp1.json")"
CODE2="$(post_build "$RESP_DIR/req2.json" "$RESP_DIR/resp2.json")"
assert_eq "首次构建 HTTP 状态" "201" "$CODE1"
assert_eq "二次构建 HTTP 状态" "201" "$CODE2"

SHA1="$(jq -r '.artifact_sha256' "$RESP_DIR/resp1.json")"
SHA2="$(jq -r '.artifact_sha256' "$RESP_DIR/resp2.json")"
HIT1="$(jq -r '.cache_hit' "$RESP_DIR/resp1.json")"
HIT2="$(jq -r '.cache_hit' "$RESP_DIR/resp2.json")"
log "制品1 SHA-256 = $SHA1 (cache_hit=$HIT1)"
log "制品2 SHA-256 = $SHA2 (cache_hit=$HIT2)"
assert_eq "首次构建未命中缓存" "false" "$HIT1"
assert_eq "二次构建命中缓存（内容寻址）" "true" "$HIT2"
assert_eq "内容相同→制品哈希一致" "$SHA1" "$SHA2"

section "5. 字节级一致性：不同 mtime + 不同目录，tar 逐字节相同"
if cmp -s "$ARTIFACT_DIR/tree1.tar" "$ARTIFACT_DIR/tree2.tar"; then
  pass "两份 tar 字节完全一致 (cmp)"
else
  fail "两份 tar 字节不一致"
fi
DISK_SHA="$(sha256sum "$ARTIFACT_DIR/tree1.tar" | awk '{print $1}')"
assert_eq "落盘文件 SHA-256 与清单一致" "$SHA1" "$DISK_SHA"

section "6. 比较摘要（剔除 build_id/时间/路径/缓存标记等易变字段）"
CANON='{status, preserve_exec, fixed_modtime_unix, artifact_sha256, artifact_size, file_count, dir_count, symlink_count, entries}'
jq "$CANON" "$RESP_DIR/resp1.json" > "$RESP_DIR/summary1.canon.json"
jq "$CANON" "$RESP_DIR/resp2.json" > "$RESP_DIR/summary2.canon.json"
if diff -u "$RESP_DIR/summary1.canon.json" "$RESP_DIR/summary2.canon.json"; then
  pass "两份规范化摘要完全一致（含逐文件 sha256/size/mode/type）"
else
  fail "规范化摘要存在差异"
fi
log "摘要计数: $(jq -c '{file_count,dir_count,symlink_count,artifact_size}' "$RESP_DIR/resp1.json")"

section "7. 用系统 tar 独立校验制品内容"
TAR_LIST="$RESP_DIR/tar-list.txt"
TZ=UTC tar -tvf "$ARTIFACT_DIR/tree1.tar" > "$TAR_LIST"
log "tar -tvf 输出（节选）:"
head -20 "$TAR_LIST" | sed 's/^/    /'

# 7.1 条目严格排序（C 字节序；比较逻辑路径，即去掉目录条目的尾随 /）
tar -tf "$ARTIFACT_DIR/tree1.tar" | sed 's:/$::' | LC_ALL=C sort -c \
  && pass "tar 条目按逻辑路径字节序严格排列" || fail "tar 条目未排序"

# 7.2 固定 mtime：全部显示 1970-01-01 00:00（UTC，GNU tar 对 :00 秒省略）
BAD_TIME="$(grep -vc '1970-01-01 00:00' "$TAR_LIST" || true)"
assert_eq "所有条目 mtime 固定为 epoch (1970-01-01 00:00 UTC)" "0" "$BAD_TIME"

# 7.3 固定权限策略：只允许三种权限串（C locale 字节序：- < d < l）
PERMS="$(awk '{print $1}' "$TAR_LIST" | LC_ALL=C sort -u | tr '\n' ' ')"
EXPECTED_PERMS="-rw-r--r-- drwxr-xr-x lrwxrwxrwx "
assert_eq "权限归一化为 0644 文件 / 0755 目录 / 0777 链接" "$EXPECTED_PERMS" "$PERMS"

# 7.4 属主全部清零：数值属主列为 0/0（不依赖 /etc/passwd 的名字映射）
TAR_LIST_NUM="$RESP_DIR/tar-list-numeric.txt"
TZ=UTC tar --numeric-owner -tvf "$ARTIFACT_DIR/tree1.tar" > "$TAR_LIST_NUM"
NUM_OWNERS="$(awk '{print $2}' "$TAR_LIST_NUM" | LC_ALL=C sort -u | tr '\n' ' ')"
assert_eq "属主统一为 0/0 (uid/gid 清零)" "0/0 " "$NUM_OWNERS"

# 7.5 覆盖 Unicode 路径 / 空目录 / 长文件名
tar -tf "$ARTIFACT_DIR/tree1.tar" > "$RESP_DIR/tar-names.txt"
assert_cmd "包含 Unicode 文件 b-ünïcode-文件.txt" grep -q 'b-ünïcode-文件.txt' "$RESP_DIR/tar-names.txt"
assert_cmd "包含 Unicode 文件 γ.txt"      grep -q 'γ.txt' "$RESP_DIR/tar-names.txt"
assert_cmd "包含 Unicode 目录 空目录-测试/" grep -q '空目录-测试/' "$RESP_DIR/tar-names.txt"
assert_cmd "包含长文件名条目 (>100 字节, PAX)"  grep -q '^nested/long-あ' "$RESP_DIR/tar-names.txt"
LONGEST="$(LC_ALL=C awk '{print length}' "$RESP_DIR/tar-names.txt" | sort -n | tail -1)"
if (( LONGEST > 100 )); then pass "最长归档路径 ${LONGEST} 字节 > 100（PAX 路径）"; else fail "最长路径只有 ${LONGEST} 字节"; fi

# 7.6 磁盘上 755 的脚本在未开 preserve_exec 时归档为 644
if grep -E '^-rwxr-xr-x .* 脚本\.sh$' "$TAR_LIST" >/dev/null; then
  fail "脚本.sh 可执行位未被归一化"
else
  assert_cmd "脚本.sh 被归一化为 0644" grep -E '^-rw-r--r-- .* 脚本\.sh$' "$TAR_LIST"
fi

# 7.7 解包往返：解包后文件内容 sha256 与清单条目一致
EXTRACT="$WORK_BASE/extract"
mkdir -p "$EXTRACT"
tar -xf "$ARTIFACT_DIR/tree1.tar" -C "$EXTRACT"
ENTRY_SHA="$(jq -r '.entries[] | select(.path=="a.txt") | .sha256' "$RESP_DIR/resp1.json")"
GOT_SHA="$(sha256sum "$EXTRACT/a.txt" | awk '{print $1}')"
assert_eq "解包往返后 a.txt 内容 SHA-256 与清单一致" "$ENTRY_SHA" "$GOT_SHA"
assert_cmd "空目录解包后仍存在" test -d "$EXTRACT/空目录-测试"

section "8. 逃逸符号链接必须被拒绝（HTTP 422，无制品落盘）"
EVIL="$FIXTURE_DIR/evil"
mkdir -p "$EVIL"
ln -s "../../../../etc/passwd" "$EVIL/evil-link"
EVIL_OUT="$ARTIFACT_DIR/evil.tar"
cat > "$RESP_DIR/req-evil.json" <<JSON
{"source_dir":"$EVIL","output_path":"$EVIL_OUT"}
JSON
EVIL_CODE="$(post_build "$RESP_DIR/req-evil.json" "$RESP_DIR/resp-evil.json")"
assert_eq "逃逸符号链接返回 422" "422" "$EVIL_CODE"
EVIL_ERR="$(jq -r '.error.code' "$RESP_DIR/resp-evil.json")"
assert_eq "错误码 unsafe_symlink" "unsafe_symlink" "$EVIL_ERR"
log "错误信息: $(jq -r '.error.message' "$RESP_DIR/resp-evil.json")"
if [[ -e "$EVIL_OUT" ]]; then fail "拒绝构建时不应产生输出文件"; else pass "拒绝构建时无输出文件"; fi

section "9. 其他接口行为"
# 9.1 相对路径 -> 400
CODE_REL="$(curl -sS -o "$RESP_DIR/rel.json" -w '%{http_code}' -H 'Content-Type: application/json' \
  -X POST "$BASE_URL/v1/builds" -d '{"source_dir":"relative/path","output_path":"/tmp/x.tar"}')"
assert_eq "相对 source_dir 返回 400" "400" "$CODE_REL"
assert_eq "错误码 invalid_request" "invalid_request" "$(jq -r '.error.code' "$RESP_DIR/rel.json")"

# 9.2 查询不存在的构建 -> 404
CODE_404="$(curl -sS -o "$RESP_DIR/404.json" -w '%{http_code}' "$BASE_URL/v1/builds/no-such-id")"
assert_eq "查询未知构建返回 404" "404" "$CODE_404"

# 9.3 列表接口包含两次成功构建
LIST_CODE="$(curl -sS -o "$RESP_DIR/list.json" -w '%{http_code}' "$BASE_URL/v1/builds")"
assert_eq "列表接口 200" "200" "$LIST_CODE"
LIST_N="$(jq '.builds | length' "$RESP_DIR/list.json")"
assert_eq "列表包含 2 次成功构建（逃逸那次不入清单）" "2" "$LIST_N"

section "10. 工作目录与缓存目录物理分离"
assert_cmd "工作目录保存清单 JSON"  bash -c "find '$WORK_DIR/manifests' -name '*.json' | grep -q ."
assert_cmd "缓存目录保存内容寻址 tar" bash -c "find '$CACHE_DIR/artifacts' -name '*.tar' | grep -q ."
if find "$WORK_DIR" -name '*.tar' | grep -q .; then fail "工作目录中混入了 tar 制品"; else pass "工作目录中无 tar 制品"; fi
if find "$CACHE_DIR" -name '*.json' | grep -q .; then fail "缓存目录中混入了清单"; else pass "缓存目录中无清单"; fi
N_CACHE_OBJ="$(find "$CACHE_DIR/artifacts" -name '*.tar' | wc -l | tr -d ' ')"
assert_eq "内容相同的多次构建只产生 1 个缓存对象" "1" "$N_CACHE_OBJ"

section "11. 清理关闭"
cleanup
if kill -0 "$SERVER_PID" 2>/dev/null; then fail "服务未正常退出"; else pass "服务收到 SIGTERM 后正常退出"; fi
SERVER_PID=""

section "验收结果"
log "PASS: $PASS_COUNT    FAIL: $FAIL_COUNT"
if (( FAIL_COUNT == 0 )); then
  log "✅ 全部验收项通过"
else
  log "❌ 存在未通过项，请检查日志: $LOG"
fi
log "完整产物目录: $WORK_BASE"
} 2>&1 | tee "$LOG"

exit "$FAIL_COUNT"
