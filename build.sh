#!/usr/bin/env bash
# Compile main sources into ./out (requires JDK 17+).
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${JAVA_HOME:-}" ]; then JAVAC="$JAVA_HOME/bin/javac"; else JAVAC="javac"; fi
$JAVAC -version

rm -rf out
mkdir -p out
find src -name '*.java' > sources.txt
$JAVAC -Xlint:all -Werror -d out @sources.txt
rm -f sources.txt
echo "build OK -> out/"
