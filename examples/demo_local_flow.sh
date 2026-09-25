#!/usr/bin/env bash
# 完整本地流程演示：keygen → sign → verify → run（含攻击场景验证）。
# 所有产物落在 ./_demo_run/ 下；不联网、不访问任何生产账号。
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=./_demo_run
rm -rf "$WORK"
mkdir -p "$WORK/artifact/bin"

cat > "$WORK/artifact/bin/hello.sh" <<'SH'
#!/bin/sh
echo "hello from signed artifact, args=$*"
SH
chmod 755 "$WORK/artifact/bin/hello.sh"
echo '{"mode":"local"}' > "$WORK/artifact/conf_placeholder" 2>/dev/null || true
mkdir -p "$WORK/artifact/conf"
echo '{"mode":"local"}' > "$WORK/artifact/conf/settings.json"
rm -f "$WORK/artifact/conf_placeholder"
echo "demo-payload" > "$WORK/artifact/data.txt"

export PYTHONPATH=.
PY="python3 -m sig_manifest"

echo "--- keygen"
$PY keygen --private-key "$WORK/key.pem" --trust-store "$WORK/trust.json" --no-password

echo "--- sign"
$PY sign --artifact-root "$WORK/artifact" --name demo-app \
  --private-key "$WORK/key.pem" --no-password \
  --entrypoint bin/hello.sh from-manifest --output "$WORK/manifest.json"

echo "--- verify"
$PY verify --artifact-root "$WORK/artifact" \
  --manifest "$WORK/manifest.json" --trust-store "$WORK/trust.json"

echo "--- run（验证通过才执行）"
$PY run --artifact-root "$WORK/artifact" \
  --manifest "$WORK/manifest.json" --trust-store "$WORK/trust.json" \
  --no-password extra-arg

echo
echo "--- 攻击演示：篡改 data.txt 后 verify 与 run 都必须失败 ---"
echo "TAMPERED" > "$WORK/artifact/data.txt"
set +e
$PY verify --artifact-root "$WORK/artifact" \
  --manifest "$WORK/manifest.json" --trust-store "$WORK/trust.json"
echo "verify exit=$?（期望 1）"
$PY run --artifact-root "$WORK/artifact" \
  --manifest "$WORK/manifest.json" --trust-store "$WORK/trust.json" \
  --no-password
echo "run exit=$?（期望非 0，且上面没有 hello 输出）"
