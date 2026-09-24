#!/usr/bin/env bash
# 端到端验收脚本：启动服务 -> 健康检查 -> 核对示例 SBOM -> 校验关键结论。
# 用法: ./scripts/acceptance.sh [PORT]
set -euo pipefail

PORT="${1:-8791}"
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PY="$HERE/.venv/bin/python"
OUT="$(mktemp -d)"
trap 'kill "$SRV_PID" 2>/dev/null || true; rm -rf "$OUT"' EXIT

# 全新数据库，保证验收可重复
rm -f data/sbom.db
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  > "$OUT/server.log" 2>&1 &
SRV_PID=$!

echo "[1/5] waiting for server on $BASE ..."
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" > "$OUT/health.json" 2>/dev/null; then break; fi
  sleep 0.2
done
[ -s "$OUT/health.json" ] || { echo "server failed to start"; cat "$OUT/server.log"; exit 1; }

echo "[2/5] health / data-source disclaimer:"
"$PY" -c "import json;d=json.load(open('$OUT/health.json'));assert d['status']=='ok';assert d['data_source']['not_real_advisory'] is True;print('  fixture vulns:',d['data_source']['vulnerability_count'],'| disclaimer present')"

echo "[3/5] POST examples/sbom-full.json"
code=$(curl -s -o "$OUT/result.json" -w '%{http_code}' -X POST \
  "$BASE/api/v1/sbom/check" -H 'content-type: application/json' \
  --data-binary @examples/sbom-full.json)
[ "$code" = "200" ] || { echo "expected 200, got $code"; cat "$OUT/result.json"; exit 1; }
echo "  HTTP 200"

echo "[4/5] verify SHA-256 equals real hash of raw request bytes"
want=$(sha256sum examples/sbom-full.json | cut -d' ' -f1)
"$PY" - "$OUT/result.json" "$want" <<'PYCHECK'
import json, sys
path, want = sys.argv[1], sys.argv[2]
r = json.load(open(path))
got = r["run"]["source_sha256"]
assert got == want, f"sha256 mismatch: {got} != {want}"
print("  sha256 ok:", got)
PYCHECK

echo "[5/5] verify structural version-range conclusions (not string compare)"
"$PY" - "$OUT/result.json" <<'PYCHECK'
import json, sys
r = json.load(open(sys.argv[1]))
s = r["summary"]
# 关键计数
assert s["cycles_detected"] == 1, s
assert s["duplicates_merged"] == 1, s
assert s["unknown_components"] == 1, s
affected = {a["component"]["purl"]: a for a in r["results"]["affected"]}
unknown  = {u["component"]["purl"]: u for u in r["results"]["unknown"]}
clean    = {n["component"]["purl"]: n for n in r["results"]["not_affected"]}

# 范围端点：修复版本不受影响
assert "pkg:maven/com.example/http-core@3.2.0" in clean
assert "pkg:maven/com.example/http-core@3.1.0" in affected
# npm 预发布门禁
assert "pkg:npm/pre-release-lib@1.0.0-rc.2" in affected
assert "pkg:npm/pre-release-lib@1.0.0" in clean
# PEP 440 数值顺序：2.21rc1 < 2.21 不命中 <2.21；2.20rc1 命中
assert "pkg:pypi/requests@2.21rc1" in clean
assert "pkg:pypi/requests@2.20rc1" in affected
# 变体不混同：linux 专属漏洞只命中 linux 变体
def ids(p): return {m["vulnerability_id"] for m in affected[p]["matched"]}
assert "SYNTH-NPM-2026-0007" in ids("pkg:npm/left-pad@6.10.3?os=linux")
assert "SYNTH-NPM-2026-0007" not in ids("pkg:npm/left-pad@6.10.3")
assert "SYNTH-NPM-2026-0007" not in ids("pkg:npm/left-pad@6.10.3?os=darwin")
# 传递依赖证据路径
curl = affected["pkg:deb/debian/curl@2.50.3-1?arch=amd64"]
assert curl["is_transitive"]
assert any("pkg:pypi/requests@2.20" in " -> ".join(p["purls"])
           for p in curl["evidence_paths"])
# 不支持生态 -> 未知
assert "pkg:cargo/ghost@0.1.0" in unknown
# 错误 purl / 缺失版本被如实报告
ign = {(i["bom_ref"], i["reason"]) for i in r["ignored_components"]}
assert ("bad-purl-1", "invalid_purl") in ign
assert ("no-version-lib", "missing_version") in ign
print("  all", s["affected_components"], "affected /",
      s["unknown_components"], "unknown /", s["not_affected_components"], "clean verified")
PYCHECK

echo
echo "ACCEPTANCE PASSED  (run id persisted; GET $BASE/api/v1/runs)"
