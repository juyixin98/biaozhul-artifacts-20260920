#!/usr/bin/env bash
# Runnable end-to-end sample against a local server.
#
#   bash examples/sample_requests.sh
#
# It creates a fresh workspace, uploads three real texts (see
# kex/services/demo_corpus.py), waits for the background worker(s) to finish,
# then demonstrates entities, search, keywords and export. Results are
# computed at runtime — nothing here is a fixed/fixture response.
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
PY="${PYTHON:-python3}"

$PY - "$BASE" <<'PYEOF'
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")


def req(method: str, path: str, key: str | None = None, body=None, raw=None,
        ctype="application/json", parse_json=True):
    url = BASE + path
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = ctype
    elif raw is not None:
        data = raw.encode("utf-8")
        headers["Content-Type"] = ctype
    if key:
        headers["X-Workspace-Key"] = key
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            text = resp.read().decode("utf-8")
            if not parse_json:
                return resp.status, text
            return resp.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


print("== health ==")
print(req("GET", "/health")[1])

st, ws = req("POST", "/api/workspaces", body={"name": f"sample-{int(time.time())}"})
assert st == 201, ws
ws_id, key = ws["workspace_id"], ws["api_key"]
print(f"== workspace created: id={ws_id} ==")

documents = [
    ("kafka-rollout.txt",
     "2024-03-18，小李在清华大学组织了 Apache Kafka 3.6 升级评审，"
     "Alice Chen 与 Acme Corp 确认 PostgreSQL 连接器兼容。"),
    ("conference-notes.txt",
     "On September 9, 2024, Bob Müller from Peking University presented "
     "Python 3.12 and SQLite WAL. 联合国团队计划在 二〇二五年六月三十日 发布白皮书。"),
    ("release-plan.txt",
     "二〇二五年十二月九日发布 1.0；老张负责冻结数据库变更，芳芳写文档，明哥值班。"),
]

for title, text in documents:
    st, body = req("POST", f"/api/workspaces/{ws_id}/documents", key,
                   body={"text": text, "title": title})
    assert st in (200, 202), (st, body)
    print(f"uploaded {title} -> document {body['document_id']} (job {body['extraction_job_id']})")

print("== waiting for extraction jobs ==")
while True:
    _, jobs = req("GET", f"/api/workspaces/{ws_id}/jobs", key)
    pending = any(
        c for j in jobs["jobs"] for c in (
            j["items"].get("queued", 0), j["items"].get("running", 0)
        )
    )
    if not pending:
        break
    time.sleep(0.4)

_, grouped = req("GET", f"/api/workspaces/{ws_id}/entities/grouped", key)
print("== canonical groups (original mentions preserved) ==")
print(json.dumps(grouped["groups"], ensure_ascii=False, indent=2)[:1600])

_, hits = req("GET", f"/api/workspaces/{ws_id}/search", key)
qs = BASE + f"/api/workspaces/{ws_id}/search?q="
import urllib.parse
_, hits = req("GET", f"/api/workspaces/{ws_id}/search?q=" +
              urllib.parse.quote("清华 Kafka"), key)
print("== search '清华 Kafka' ==")
for h in hits["results"]:
    print(f"  doc {h['document_id']} {h['title']} score={h['score']} gen={hits['index_generation']}")

_, kws = req("GET", f"/api/workspaces/{ws_id}/documents/2/keywords?top_k=6", key)
print("== keywords doc 2 ==")
print("  " + ", ".join(f"{k['term']}:{k['weight']:.3f}" for k in kws["keywords"]))

st, exported = req("GET", f"/api/workspaces/{ws_id}/export", key, parse_json=False)
print(f"== export ({st}), records: {len([l for l in exported.splitlines() if l.strip()])} ==")
PYEOF
