#!/usr/bin/env bash
# 编译并运行全部自动化测试；任一断言失败则以非零码退出。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
java -Dfile.encoding=UTF-8 -cp build/classes com.example.streammatch.tests.TestAll
