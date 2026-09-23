#!/usr/bin/env bash
# 运行全部自动化测试（验收场景 + 随机差分 + HTTP 端到端）。
# 任一用例失败则退出码非零。
set -euo pipefail
cd "$(dirname "$0")/.."

scripts/build.sh

resolve_java() {
  if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
    echo "$JAVA_HOME/bin/java"
  elif command -v java >/dev/null 2>&1; then
    echo "java"
  elif [ -x "$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java" ]; then
    echo "$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java"
  else
    echo "错误: 找不到 java。请安装 JDK 11+ 或设置 JAVA_HOME。" >&2
    exit 1
  fi
}
JAVA="$(resolve_java)"

rc=0
for suite in topk.AcceptanceTest topk.PropertyTest topk.HttpSmokeTest; do
  echo
  if ! "$JAVA" -cp "build/classes:build/test-classes" "$suite"; then
    rc=1
  fi
done

echo
if [ "$rc" -eq 0 ]; then
  echo "全部测试通过 ✔"
else
  echo "存在失败用例 ✘" >&2
fi
exit "$rc"
