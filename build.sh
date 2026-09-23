#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -rf out
mkdir -p out
javac -d out $(find src/main/java -name '*.java')
echo "build ok"
