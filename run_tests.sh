#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
javac -cp out -d out $(find src/test/java -name '*.java')
java -cp out com.pushdown.TestRunner
