#!/usr/bin/env bash
# Compile main sources with plain javac (no dependency manager needed).
set -euo pipefail
cd "$(dirname "$0")"
source ./scripts-common.sh

mkdir -p target/classes
find src/main/java -name '*.java' > target/sources.txt
"$JAVAC" -Xlint:all -d target/classes @target/sources.txt
rm -f target/ordered-events.jar
"$JAR" --create --file target/ordered-events.jar \
  --main-class io.example.orderedcommit.Main -C target/classes .
echo "built target/ordered-events.jar ($("$JAVA" -version 2>&1 | head -1))"
