# HTTP API 参考

基础路径：`http://localhost:8000`
除 `POST /api/workspaces` 与 `GET /health` 外，所有路由都需要请求头：

```
X-Workspace-Key: <创建工作区时返回的 api_key>
```

鉴权失败返回 `401`；访问他人工作区的文档/作业一律返回 `404`（不区分不存在与
无权访问，避免探测）。

## 健康检查

### GET /health

```json
{ "status": "ok", "journal_mode": "wal" }
```

## 工作区

### POST /api/workspaces

请求：`{"name": "demo"}` → `201`

```json
{
  "workspace_id": 1,
  "name": "demo",
  "api_key": "6lH4h1m...（仅本次返回）",
  "warning": "save this key now; it is never returned again"
}
```

创建时自动发布内置词典规则 v1（不可变）并激活空的索引第 1 代。

## 文档

### POST /api/workspaces/{ws}/documents

两种提交方式：

- `Content-Type: application/json`：`{"text": "UTF-8 文本", "title": "可选"}`
- `Content-Type: text/plain`：请求体即文本；`?title=` 可带标题

非 UTF-8 字节 → `400 {"error": "only UTF-8 text is accepted: ..."}`。

响应：新文档 `202`；相同内容在同工作区已存在 `200`（复用，不产生新作业）：

```json
{
  "document_id": 1,
  "title": "kafka-rollout.txt",
  "deduplicated": false,
  "extraction_job_id": 4,
  "status_url": "/api/workspaces/1/jobs/4"
}
```

### GET /api/workspaces/{ws}/documents?limit=&offset=

### GET /api/workspaces/{ws}/documents/{id}

返回 `{document_id,title,sha256,length,content}`。

### DELETE /api/workspaces/{ws}/documents/{id}

同事务内：从该工作区每个索引代（active/building/retired）删除 posting、
term/统计，删除实体与排队 item，删除工作区关联；当且仅当全局 blob 引用计数为 0
时删除内容本身。响应 `{"deleted": 1}`。之后该文档不会出现在搜索、实体、导出中。

## 实体与导出

### GET /api/workspaces/{ws}/entities

查询参数：`document_id`、`type=PERSON|ORG|TECH|DATE`、`rule_version`（默认当前
激活版本）、`limit`、`offset`。

```json
{
  "total": 6,
  "rule_version": null,
  "entities": [
    {
      "entity_id": 12,
      "document_id": 1,
      "entity_type": "PERSON",
      "text": "小李",
      "canonical_name": "李明",
      "start_char": 0,
      "end_char": 2,
      "matched_rule": "gazetteer.person.liming"
    }
  ]
}
```

`text[start_char:end_char]` 恒等于原文中的提及片段；`canonical_name` 是归一化
结果，原文提及不丢失。

### GET /api/workspaces/{ws}/entities/grouped?rule_version=

按规范名分组，组内保留全部原始 surface form、位置与命中规则。

### GET /api/workspaces/{ws}/export?rule_version=

`application/x-ndjson`：每行一个存活文档及其全部实体。已删除文档天然不出现。

## 检索

### GET /api/workspaces/{ws}/search?q=关键词&limit=20&offset=0

```json
{
  "query": "清华",
  "index_generation": 3,
  "index_rule_version_id": 5,
  "total": 2,
  "results": [
    {"document_id": 1, "title": "a.txt", "sha256": "…", "score": 0.3208}
  ]
}
```

- 只读取当前 active 的那一**代**完整索引；重建期间继续返回旧代结果。
- 排序：score 降序，平手按 document_id 升序（稳定可复现）。

### GET /api/workspaces/{ws}/documents/{id}/keywords?top_k=20

该文档在 active 代中的 TF-IDF 关键词，权重降序、平手按词升序。

## 规则版本

### GET /api/workspaces/{ws}/rules

列出全部版本（版本号、校验和、是否 active、创建时间）。已发布版本不可修改。

### POST /api/workspaces/{ws}/rules/publish

请求：`{"rules": { …规则集… }, "note": "可选"}`（也可直接把规则集放在顶层）。

- 校验失败（别名冲突映射、非法正则等）→ `400`；
- 内容与某既有版本完全相同（SHA-256 一致）→ `200`，不重建；
- 新版本 → `202`：规则指针立即指向新版本，同时创建 `building` 索引代与重建作业，
  搜索继续读旧 active 代，作业全部成功后原子切换。

### POST /api/workspaces/{ws}/rules/rollback

请求：`{"version": 1}`。

- 存在该规则构建过的完整代（active/retired）→ `200` 直接切代；
- 不存在 → `202`，用该不可变规则重新构建一代；
- 无论哪种，历史实体证据与旧代都不修改/不删除（`history_rewritten: false`）。

## 作业

### GET /api/workspaces/{ws}/jobs

### GET /api/workspaces/{ws}/jobs/{id}

```json
{
  "job_id": 7,
  "kind": "rebuild",
  "status": "partial",
  "rule_version_id": 5,
  "index_generation_id": 9,
  "items": {"total": 12, "done": 11, "failed": 1},
  "failed_items": [
    {"item_id": 43, "document_id": 8, "stage": "index",
     "error": "RuntimeError: simulated disk hiccup at index stage",
     "attempts": 1, "document_sha256": "ab12…"}
  ],
  "error": "1 item(s) failed; POST /jobs/{id}/retry",
  "created_at": "…", "finished_at": null
}
```

`stage ∈ queued|extract|index|done`，用于崩溃后定位进度。

### POST /api/workspaces/{ws}/jobs/{id}/retry

将作业的 failed item 重新排队，作业回到 queued；已成功的 item 不重做。
