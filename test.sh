#!/usr/bin/env bash
# Build main + tests, then run the JUnit-free test harness.
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${JAVA_HOME:-}" ]; then
  JAVAC="$JAVA_HOME/bin/javac"; JAVA="$JAVA_HOME/bin/java"
else
  JAVAC="javac"; JAVA="java"
fi

rm -rf out
mkdir -p out
find src test -name '*.java' > sources.txt
$JAVAC -Xlint:all -Werror -d out @sources.txt
rm -f sources.txt

$JAVA -cp out colscan.TestRunner
