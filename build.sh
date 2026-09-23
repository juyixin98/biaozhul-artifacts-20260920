#!/usr/bin/env bash
# Compile main sources with plain javac. No third-party dependencies.
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-}"
if [ -n "$JAVA_HOME" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="javac"
fi

rm -rf target/classes
mkdir -p target/classes

find src/main/java -name '*.java' > target/sources.txt
"$JAVAC" --release 17 -encoding UTF-8 -Xlint:all -d target/classes @target/sources.txt
echo "Built target/classes"
