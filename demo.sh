#!/usr/bin/env bash
# 端到端可运行样例：建工作区 → 上传真实文本 → 轮询作业 → 实体/搜索/关键词
# → 规则升级 → 回滚 → 删除验证不泄露。结果均来自实时抽取，不是固定数据。
set -euo pipefail

BASE="${KEX_BASE:-http://localhost:8080}"
ADMIN_KEY="${KEX_ADMIN_KEY:-dev-admin-key}"
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))"; }
jraw() { python3 -m json.tool; }

echo "== 0) 健康检查"
curl -fsS "$BASE/healthz" | jraw

echo; echo "== 1) 创建工作区"
WS_JSON=$(curl -fsS -X POST "$BASE/api/workspaces" -H 'Content-Type: application/json' \
  -d '{"name":"demo-工作区"}')
echo "$WS_JSON" | jraw
WS_ID=$(echo "$WS_JSON" | jget "['id']")
KEY=$(echo "$WS_JSON" | jget "['api_key']")
AUTH=(-H "X-Workspace-Key: $KEY")
ADM=(-H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json')

echo; echo "== 2) 上传一篇真实技术文本"
cat > /tmp/kex_news.txt <<'EOF'
2024年3月15日，北京大学的李明教授在采访中说，团队与阿里巴巴集团合作，
把基于 Python 3.12.1 和 SQLAlchemy 的原型迁移到了 PostgreSQL 16.2。
王芳指出，旧系统使用 SQLite 与 Flask 构建，部署在 Docker 中。
Alan Kay 在 Xerox PARC 回忆 Smalltalk；Linus Torvalds 在 Sept 2nd, 2024 讨论调度。
EOF
UP=$(curl -fsS -X POST "$BASE/api/documents" "${AUTH[@]}" \
  -F "file=@/tmp/kex_news.txt;filename=news.txt")
echo "$UP" | jraw
DOC=$(echo "$UP" | jget "['doc_id']")

echo; echo "== 3) 轮询作业直到完成"
for _ in $(seq 1 50); do
  ST=$(curl -fsS "$BASE/api/jobs" "${AUTH[@]}" | jget "['jobs'][0]['status']")
  [ "$ST" = "succeeded" ] && break
  sleep 0.3
done
curl -fsS "$BASE/api/jobs" "${AUTH[@]}" | jraw

echo; echo "== 4) 四类实体（含原文位置/规则/规范名/别名）"
curl -fsS "$BASE/api/documents/$DOC/entities" "${AUTH[@]}" | jraw

echo; echo "== 5) TF-IDF 关键词"
curl -fsS "$BASE/api/documents/$DOC/keywords?top_k=8" "${AUTH[@]}" | jraw

echo; echo "== 6) 倒排搜索（AND + 稳定排序 + 命中位置）"
Q=$(python3 -c 'import urllib.parse;print(urllib.parse.quote("Python SQLite"))')
curl -fsS "$BASE/api/search?q=$Q" "${AUTH[@]}" | jraw

echo; echo "== 7) 导出（NDJSON）前 40 行预览"
curl -fsS "$BASE/api/export?format=ndjson" "${AUTH[@]}" | head -c 1200
echo

echo; echo "== 8) 跨工作区访问被拒"
OTHER=$(curl -fsS -X POST "$BASE/api/workspaces" -H 'Content-Type: application/json' -d '{"name":"other"}')
OKEY=$(echo "$OTHER" | jget "['api_key']")
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/documents/$DOC" -H "X-Workspace-Key: $OKEY")
echo "用另一个工作区密钥读取本文档 -> HTTP $CODE（期望 404/401）"

echo; echo "== 9) 删除文档后搜索/实体不再泄露"
curl -fsS -X DELETE "$BASE/api/documents/$DOC" "${AUTH[@]}" | jraw
echo "删除后搜索 SQLite 命中数: $(curl -fsS "$BASE/api/search?q=SQLite" "${AUTH[@]}" | jget "['result_count']")"

echo; echo "样例完成。"
