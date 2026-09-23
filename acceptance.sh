#!/usr/bin/env bash
# 一键本地验收: 启动 Postgres(若需要) -> 安装依赖 -> 全量测试 -> 启动服务
# -> 签名播种示例 -> 检查关键行为(陈旧价格/清算/版本验签) -> 关闭服务。
set -euo pipefail

cd "$(dirname "$0")"
DB_URL="${LIQREPLAY_DATABASE_URL:-postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay}"
export LIQREPLAY_DATABASE_URL="$DB_URL"
PORT=8000

echo "== 1/6 虚拟环境与依赖 =="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== 2/6 启动 Postgres (若尚未运行) =="
if docker ps --format '{{.Names}}' | grep -q '^liqreplay-db$'; then
  echo "liqreplay-db already running"
else
  docker rm -f liqreplay-db >/dev/null 2>&1 || true
  docker run -d --name liqreplay-db \
    -e POSTGRES_USER=liqreplay -e POSTGRES_PASSWORD=liqreplay \
    -e POSTGRES_DB=liqreplay -p 55460:5432 postgres:16-alpine >/dev/null
fi
for i in $(seq 1 30); do
  docker exec liqreplay-db pg_isready -U liqreplay >/dev/null 2>&1 && break
  sleep 1
done

echo "== 3/6 自动化测试 (含 PostgreSQL 仓储测试) =="
LIQREPLAY_TEST_DATABASE_URL="$DB_URL" .venv/bin/pytest

echo "== 3b/6 清空旧数据 (测试库), 保证播种从头开始 =="
.venv/bin/python - <<'PY'
import os
import psycopg

url = os.environ["LIQREPLAY_DATABASE_URL"]
with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("TRUNCATE events RESTART IDENTITY CASCADE")
    cur.execute("TRUNCATE reports RESTART IDENTITY CASCADE")
    conn.commit()
print("database truncated")
PY

echo "== 4/6 生成操作者密钥 =="
.venv/bin/python scripts/generate_keys.py || true
OPERATOR_PUB=$(.venv/bin/python -c "from app.crypto import load_private_key; from app.config import settings; print(load_private_key(settings.keys_dir/'operator_ed25519.pem').public_key().public_bytes(__import__('cryptography').hazmat.primitives.serialization.Encoding.Raw, __import__('cryptography').hazmat.primitives.serialization.PublicFormat.Raw).hex())")
export LIQREPLAY_OPERATOR_KEYS="$OPERATOR_PUB"
echo "operator pub: $OPERATOR_PUB"

echo "== 5/6 启动服务 =="
LIQREPLAY_DATABASE_URL="$DB_URL" LIQREPLAY_OPERATOR_KEYS="$OPERATOR_PUB" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  >data/server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for i in $(seq 1 30); do
  curl --noproxy '*' -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl --noproxy '*' -s "http://127.0.0.1:$PORT/health" | .venv/bin/python -m json.tool

echo "== 6/6 播种示例事件并校验 =="
.venv/bin/python scripts/seed.py --base-url "http://127.0.0.1:$PORT" | tee data/seed.json
.venv/bin/python - "$PORT" <<'PY'
import json, sys, urllib.request

base = f"http://127.0.0.1:{sys.argv[1]}"


def get(path):
    # 本机服务, 显式绕过环境里可能存在的代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path) as r:
        return json.load(r)


raw = open("data/seed.json").read()
seed = json.loads(raw[raw.index("{"):])
assert seed["accepted"] == 12, seed
assert seed["latest_version"] == 1

report = get("/api/v1/reports/latest")["body"]
liqs = report["liquidations"]
# A 被清算两次, 每次 2000; B 抵押不足部分成交
assert [x["position_id"] for x in liqs] == ["A", "A", "B"], liqs
assert liqs[0]["seized_collateral"] == int(2.4e18), liqs[0]
assert liqs[2]["partial_fill"] is True
assert liqs[2]["seized_collateral"] == 10**15

reasons = {(a["event_id"], a.get("reason")) for a in report["actions"]}
assert ("ev-liq-stale", "STALE_PRICE") in reasons, reasons

v = get("/api/v1/reports/1/verify")
assert v["report_signature_valid"] and v["hash_matches_body"], v
print("ACCEPTANCE OK: 利率切换/健康度/重复清算/部分成交/陈旧价格/版本验签 全部通过")
PY

echo "完成。服务日志: data/server.log, 播种响应: data/seed.json"
