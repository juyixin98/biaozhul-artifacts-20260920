#!/usr/bin/env bash
# cdbg 验收脚本：用 CLI 在隔离目录中实际运行测试夹具命令。
# 覆盖：
#   1) 首次全量构建 -> 2) 二次全缓存命中
#   3) 只改 mtime（内容不变）-> 全部命中
#   4) 改参数（= 改变生成文件内容），保持旧 mtime -> 传递链重建、无关节点复用
#   5) 改输入文件内容、保持旧 mtime -> 精确重建
#   6) 工具版本变化 -> 仅使用者重建
#   7) 失败节点不发布缓存，下游跳过
#   8) 依赖环检测
#   9) dry-run/explain 不执行任何命令
#  10) 缓存目录与工作目录分离
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
WORK="$TMP/work"
CACHE="$TMP/cache"
STATE="$TMP/state"
SPEC="$TMP/spec.json"
mkdir -p "$WORK"

PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }

check() { # check <描述> <条件命令...>
  local desc="$1"; shift
  if "$@"; then
    green "  PASS: $desc"
    PASS=$((PASS+1))
  else
    red "  FAIL: $desc"
    FAIL=$((FAIL+1))
  fi
}

# 从最近一次 run 的 JSON 中取节点状态: status <node>
STATUS_FILE="$TMP/last.json"
status() { jq -r --arg n "$1" '.nodes[] | select(.name==$n) | .status' "$STATUS_FILE"; }
reasons() { jq -r --arg n "$1" '.nodes[] | select(.name==$n) | .reasons[]' "$STATUS_FILE"; }
check_reason() { # check_reason <节点> <关键词>
  local node="$1" needle="$2"
  if reasons "$node" | grep -qF "$needle"; then
    green "  PASS: $node 解释含『$needle』"
    PASS=$((PASS+1))
  else
    red "  FAIL: $node 解释含『$needle』"
    FAIL=$((FAIL+1))
  fi
}

run_build() { # run_build [额外 cdbg run 参数...]
  "$ROOT/cdbg" run -spec "$SPEC" -workdir "$WORK" -cachedir "$CACHE" -statedir "$STATE" "$@" \
    > "$STATUS_FILE" 2>"$TMP/run.err"
  echo $? > "$TMP/run.rc"
}

echo "== 构建 cdbg =="
( cd "$ROOT" && go build -o cdbg ./cmd/cdbg ) || { red "构建失败"; exit 1; }

cat > "$SPEC" <<'JSON'
{
  "version": "1",
  "tools": {
    "write": {
      "name": "write", "tool_version": "1.0.0", "shell": true,
      "command": ["mkdir -p \"$(dirname \"$1\")\" && printf '%s' \"$2\" > \"$1\""]
    },
    "concat": {
      "name": "concat", "tool_version": "1.0.0", "shell": true,
      "command": ["cat \"$1\" \"$2\" > \"$3\""]
    },
    "sha": {
      "name": "sha", "tool_version": "1.0.0", "shell": true,
      "command": ["sha256sum \"$1\" > \"$2\""]
    },
    "fail": {
      "name": "fail", "tool_version": "1.0.0", "shell": true,
      "command": ["echo boom >&2; exit 7"]
    }
  },
  "nodes": [
    {"name": "gen_src",  "tool": "write", "outputs": ["src.txt"],
     "params": {"c": "SRC1"}, "args": ["src.txt", "{{.c}}"]},
    {"name": "gen_hdr",  "tool": "write", "outputs": ["hdr.txt"],
     "params": {"c": "HDR1"}, "args": ["hdr.txt", "{{.c}}"]},
    {"name": "merge",    "tool": "concat", "deps": ["gen_src", "gen_hdr"],
     "outputs": ["merged.txt"], "args": ["hdr.txt", "src.txt", "merged.txt"]},
    {"name": "hash",     "tool": "sha", "deps": ["merge"],
     "outputs": ["merged.sha"], "args": ["merged.txt", "merged.sha"]},
    {"name": "solo",     "tool": "write", "outputs": ["solo.txt"],
     "params": {"c": "SOLO"}, "args": ["solo.txt", "{{.c}}"]}
  ]
}
JSON

echo
echo "== 1) 首次全量构建 =="
run_build
rc=$(cat "$TMP/run.rc")
check "首次构建退出码 0" test "$rc" -eq 0
check "gen_src 实际执行 (built)" test "$(status gen_src)" = built
check "hash 实际执行 (built)"    test "$(status hash)" = built
check "输出文件 merged.txt 存在" test -f "$WORK/merged.txt"

echo
echo "== 2) 第二次构建全部命中缓存 =="
run_build
check "退出码 0" test "$(cat "$TMP/run.rc")" -eq 0
for n in gen_src gen_hdr merge hash solo; do
  check "$n 缓存命中 (cached)" test "$(status "$n")" = cached
done

echo
echo "== 3) 只修改时间戳，内容不变 =="
touch -d '2000-01-01' "$WORK"/*.txt "$WORK"/*.sha 2>/dev/null || true
run_build
for n in gen_src gen_hdr merge hash solo; do
  check "$n 仅 touch mtime 后仍命中" test "$(status "$n")" = cached
done

echo
echo "== 4) 修改参数（文件内容随之变化），保持旧 mtime：传递依赖重建，无关节点复用 =="
# 改 gen_src 的参数 SRC1 -> SRC2
sed -i 's/SRC1/SRC2/' "$SPEC"
# 保留 spec 之外工作文件的旧 mtime（命令重建后会更新被写文件，但判定只看内容哈希）
run_build
check "gen_src 重建" test "$(status gen_src)" = built
check "merge 因传递依赖重建" test "$(status merge)" = built
check "hash 因传递依赖重建"  test "$(status hash)" = built
check "gen_hdr 无关，复用缓存" test "$(status gen_hdr)" = cached
check "solo 无关，复用缓存"    test "$(status solo)" = cached
check_reason merge "依赖缓存键变化"
check_reason gen_src "参数变化"
check "merged.txt 内容已准确更新为 SRC2"   grep -q SRC2 "$WORK/merged.txt"
check "merged.sha 是新 merged.txt 的真实哈希" sh -c "cd '$WORK' && sha256sum -c merged.sha >/dev/null 2>&1"

echo
echo "== 5) 改回旧内容：命中内容寻址缓存（无需重新执行） =="
sed -i 's/SRC2/SRC1/' "$SPEC"
run_build
check "gen_src 改回后复用旧缓存条目 (cached)" test "$(status gen_src)" = cached
check "merge 改回后复用旧缓存条目 (cached)"    test "$(status merge)" = cached
check "solo 仍复用"      test "$(status solo)" = cached
grep -q SRC1 "$WORK/merged.txt" && green "  PASS: 内容回到 SRC1" || { red "  FAIL: 内容回到 SRC1"; FAIL=$((FAIL+1)); }

echo
echo "== 5b) 输入文件内容变更但保持旧 mtime：精确失效 =="
cat > "$TMP/spec-input.json" <<'JSON'
{
  "version": "1",
  "tools": {"copy": {"name": "copy", "tool_version": "1", "shell": true,
    "command": ["cp \"$1\" \"$2\""]}},
  "nodes": [{"name": "cp", "tool": "copy", "inputs": ["input.txt"],
    "outputs": ["copied.txt"], "args": ["input.txt", "copied.txt"]}]
}
JSON
printf 'one' > "$WORK/input.txt"
INPUT_RUN() { "$ROOT/cdbg" run -spec "$TMP/spec-input.json" -workdir "$WORK" -cachedir "$CACHE" -statedir "$STATE" "$@" > "$STATUS_FILE" 2>/dev/null; }
INPUT_RUN
check "输入拷贝首次执行" test "$(status cp)" = built
# 只 touch mtime
touch -d '2001-02-03 04:05:06' "$WORK/input.txt"
INPUT_RUN
check "仅 mtime 变化时命中" test "$(status cp)" = cached
# 改内容并把 mtime 改回同一旧时刻
printf 'two' > "$WORK/input.txt"
touch -d '2001-02-03 04:05:06' "$WORK/input.txt"
INPUT_RUN
check "内容变化（mtime 不变）时重建" test "$(status cp)" = built
check_reason cp "输入内容变化"
check "输出反映新内容" sh -c "test \"$(cat "$WORK/copied.txt")\" = two"

echo
echo "== 6) 工具版本变化：仅使用者重建 =="
sed -i 's/"name": "concat", "tool_version": "1.0.0"/"name": "concat", "tool_version": "2.0.0"/' "$SPEC"
run_build
check "merge 因工具版本变化重建" test "$(status merge)" = built
check_reason merge "工具版本变化"
check "hash 因依赖键变化重建"     test "$(status hash)" = built
check "gen_src 不重建"           test "$(status gen_src)" = cached
check "solo 不重建"              test "$(status solo)" = cached

echo
echo "== 7) 失败节点不发布缓存，下游跳过 =="
sed -i 's/"name": "merge",    "tool": "concat"/"name": "merge",    "tool": "fail"/' "$SPEC"
run_build
rc=$(cat "$TMP/run.rc")
check "失败构建退出码非 0" test "$rc" -ne 0
check "merge 状态 failed" test "$(status merge)" = failed
check "hash 状态 skipped" test "$(status hash)" = skipped
check_reason merge "不发布缓存"
# 恢复 merge 为 concat（版本保持 2.0.0）后，merge 应重新执行而非命中
sed -i 's/"name": "merge",    "tool": "fail"/"name": "merge",    "tool": "concat"/' "$SPEC"
run_build
check "恢复后 merge 重新执行（上次失败未发布）" test "$(status merge)" = built
check "恢复后整图成功" test "$(cat "$TMP/run.rc")" -eq 0

echo
echo "== 8) 依赖环检测 =="
cp "$SPEC" "$TMP/spec.bak.json"
# gen_src 增加对 hash 的依赖 => gen_src->merge->hash->gen_src
python3 - "$SPEC" <<'PY'
import json, sys
p = sys.argv[1]
s = json.load(open(p))
for n in s["nodes"]:
    if n["name"] == "gen_src":
        n["deps"] = ["hash"]
json.dump(s, open(p, "w"))
PY
run_build
check "环导致非零退出码" test "$(cat "$TMP/run.rc")" -ne 0
check "结果 JSON 含 cycle 字段" jq -e '.cycle | length > 0' "$STATUS_FILE" >/dev/null
cp "$TMP/spec.bak.json" "$SPEC"

echo
echo "== 9) dry-run 不执行、不产生输出 =="
rm -f "$WORK/dryrun-marker"
cat > "$TMP/spec-dry.json" <<'JSON'
{
  "version": "1",
  "tools": {"write": {"name": "write", "tool_version": "1", "shell": true,
    "command": ["printf '%s' \"$2\" > \"$1\""]}},
  "nodes": [{"name": "d", "tool": "write", "outputs": ["dryrun-marker.txt"],
    "params": {"c": "X"}, "args": ["dryrun-marker.txt", "{{.c}}"]}]
}
JSON
"$ROOT/cdbg" run -spec "$TMP/spec-dry.json" -workdir "$WORK" -cachedir "$CACHE" -statedir "$STATE" -dry-run > "$STATUS_FILE" 2>/dev/null
check "dry-run 退出码 0" test $? -eq 0
check "dry-run 规划为 built（计划执行）" test "$(status d)" = built
check "dry-run 没有创建输出文件" test ! -e "$WORK/dryrun-marker.txt"

echo
echo "== 10) 缓存与工作目录分离 =="
check "工作目录不含 meta.json" sh -c "! find '$WORK' -name meta.json | grep -q ."
check "工作目录不含 state.json" sh -c "! find '$WORK' -name state.json | grep -q ."
check "缓存条目在独立缓存目录中" sh -c "find '$CACHE' -name meta.json | grep -q ."
check "状态文件在独立状态目录中"   sh -c "find '$STATE' -name state.json | grep -q ."

echo
echo "=================================="
green "通过: $PASS"
if [ "$FAIL" -gt 0 ]; then red "失败: $FAIL"; else echo "失败: 0"; fi
echo "临时目录: $TMP"
echo "=================================="
[ "$FAIL" -eq 0 ]
