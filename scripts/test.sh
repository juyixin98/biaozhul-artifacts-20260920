#!/usr/bin/env bash
# 运行全部自动化测试；任一测试类失败则整体以非零码退出。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  scripts/build.sh
fi

CP=build/classes
# JDK 18+ 默认 file.encoding=UTF-8，这里显式指定以保证所有环境下中文输出正常。
JAVA="java -Dfile.encoding=UTF-8 -cp $CP"

fail=0
for t in SegTest DictTest ServiceTest ServerTest; do
  echo "==================== $t ===================="
  if ! $JAVA com.example.seg.$t; then
    fail=1
  fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "全部测试通过"
else
  echo "存在失败测试"
fi
exit $fail
