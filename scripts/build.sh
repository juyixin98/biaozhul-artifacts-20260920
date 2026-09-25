#!/usr/bin/env bash
# 零依赖构建：仅需 JDK（javac/java）。产物在 build/。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[build] compiling main sources"
mkdir -p build/classes
find src/main/java -name '*.java' > build/main-sources.txt
javac -d build/classes @build/main-sources.txt

if [ "${1:-}" = "--all" ]; then
  echo "[build] compiling test sources"
  mkdir -p build/test-classes
  find src/test/java -name '*.java' > build/test-sources.txt
  javac -cp build/classes -d build/test-classes @build/test-sources.txt
fi

# 生成便捷启动脚本
cat > build/cp-tx <<EOF
#!/usr/bin/env bash
exec java -cp "\$(dirname "\$0")/classes" dev.example.cp.cli.Cli "\$@"
EOF
chmod +x build/cp-tx
echo "[build] done -> build/cp-tx"
