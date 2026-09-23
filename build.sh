#!/usr/bin/env bash
# Builds the project with the plain JDK (no Maven/Gradle, zero dependencies).
set -euo pipefail
cd "$(dirname "$0")"

SRC_DIR=src
OUT_DIR=out
JAR=hllengine.jar

echo "[build] compiling Java sources with javac..."
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
find "$SRC_DIR" -name '*.java' > sources.txt
if ! javac -encoding UTF-8 -d "$OUT_DIR" @sources.txt; then
  echo "[build] COMPILATION FAILED" >&2
  exit 1
fi

echo "[build] packaging $JAR ..."
cat > "$OUT_DIR/MANIFEST.MF" <<'EOF'
Manifest-Version: 1.0
Main-Class: hllengine.server.Main
EOF
jar cfm "$JAR" "$OUT_DIR/MANIFEST.MF" -C "$OUT_DIR" .

echo "[build] done -> $JAR"
echo "[build] run:   java -jar $JAR --help"
