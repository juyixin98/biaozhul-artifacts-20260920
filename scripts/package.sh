#!/usr/bin/env bash
# Package the service into a runnable jar (manifest Main-Class) without bundling
# any third-party code; the service runs on the JDK alone.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/build.sh

cat > build/manifest.mf <<EOF
Manifest-Version: 1.0
Main-Class: com.example.qsketch.server.HttpServerMain
EOF

jar cfm build/dist/qsketch.jar build/manifest.mf -C build/classes .
echo "[package] OK -> build/dist/qsketch.jar"
echo "run: java -jar build/dist/qsketch.jar --port 8080"
