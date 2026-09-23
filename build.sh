#!/usr/bin/env bash
# Compiles the main sources with the locked JDK (see scripts/resolve-jdk.sh).
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=scripts/resolve-jdk.sh
source scripts/resolve-jdk.sh

echo "using javac: $JAVAC"
"$JAVAC" -version

rm -rf build/classes
mkdir -p build/classes
find src/main/java -name '*.java' > build/main-sources.txt
"$JAVAC" -encoding UTF-8 -d build/classes @build/main-sources.txt
rm -f build/main-sources.txt
echo "build OK -> build/classes"
