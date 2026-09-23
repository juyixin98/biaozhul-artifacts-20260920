#!/usr/bin/env bash
# Compile every Java source into classes/. Requires JDK 17+ on PATH,
# or JAVA_HOME pointing at a JDK.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="javac"
fi

"$JAVAC" -version
mkdir -p classes build
find src -name '*.java' > build/sources.txt
"$JAVAC" -d classes @build/sources.txt
echo "Compiled $(wc -l < build/sources.txt) source files -> classes/"
