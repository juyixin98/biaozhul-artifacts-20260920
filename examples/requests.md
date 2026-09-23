### 1. 健康检查

```bash
curl -s http://localhost:8080/health
```

### 2. 第一页检索（默认 pageSize=10）

```bash
curl -s 'http://localhost:8080/search?q=apple&pageSize=2'
```

### 3. 用响应中的 nextCursor 翻下一页

```bash
# cursor 原样作为查询参数回传（需 URL 编码，curl --data-urlencode / -G 可自动处理）
curl -s -G 'http://localhost:8080/search' \
  --data-urlencode 'q=apple' \
  --data-urlencode 'pageSize=2' \
  --data-urlencode 'cursor=<上一页响应中的 nextCursor>'
```

### 4. 同分决胜验证：三篇内容完全相同的文档

```bash
curl -s 'http://localhost:8080/search?q=cherry'
# 预期顺序：dup-1, dup-2, dup-3（分数完全相等，按 docId 字典序升序）
```

### 5. 新增 / 覆盖单篇文档

```bash
curl -s -X POST 'http://localhost:8080/documents' \
  -H 'Content-Type: application/json' \
  -d '{"id":"new-apple","text":"apple crisp pie"}'
```

### 6. 批量写入

```bash
curl -s -X POST 'http://localhost:8080/documents/bulk' \
  -H 'Content-Type: application/json' \
  -d '{"documents":[{"id":"x1","text":"apple apple"},{"id":"x2","text":"banana"}]}'
```

### 7. 删除文档

```bash
curl -s -X DELETE 'http://localhost:8080/documents/doc-01'
```

### 8. 查看保留的索引快照

```bash
curl -s 'http://localhost:8080/snapshots'
```

### 9. 快照隔离（核心语义，手工验证步骤）

```bash
# (a) 取第 1 页，保存 cursor
C=$(curl -s 'http://localhost:8080/search?q=apple&pageSize=2' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')

# (b) 修改语料（新增、删除、修改均可）
curl -s -X POST 'http://localhost:8080/documents' -H 'Content-Type: application/json' \
  -d '{"id":"new-apple","text":"apple crisp"}'
curl -s -X DELETE 'http://localhost:8080/documents/doc-01'

# (c) 用旧 cursor 继续翻页：snapshotVersion / totalHits / 命中均保持变更前的样子
curl -s -G 'http://localhost:8080/search' \
  --data-urlencode 'q=apple' --data-urlencode 'pageSize=2' --data-urlencode "cursor=$C"

# (d) 不带 cursor 的新搜索使用最新快照，能看到变更
curl -s 'http://localhost:8080/search?q=apple&pageSize=20'
```

### 10. 快照过期（HTTP 410）

只保留最近 8 个快照。对同一句查询连续发生 8 次以上语料变更后再使用旧游标：

```bash
curl -s -G 'http://localhost:8080/search' \
  --data-urlencode 'q=apple' --data-urlencode 'pageSize=2' --data-urlencode "cursor=$C"
# HTTP 410 {"error":{"code":"SNAPSHOT_EXPIRED", ...}}
# 处理方式：丢弃旧游标，从第一页重新开始分页。
```

### 11. 错误样例

```bash
curl -s 'http://localhost:8080/search?pageSize=3'                 # 400 BAD_REQUEST（缺 q）
curl -s 'http://localhost:8080/search?q=apple&pageSize=999'       # 400 BAD_REQUEST（pageSize 超范围）
curl -s 'http://localhost:8080/search?q=apple&cursor=garbage'     # 400 INVALID_CURSOR
# 换查询词 / 换 pageSize 却沿用旧游标：400 CURSOR_MISMATCH
curl -s 'http://localhost:8080/no-such-route'                     # 404 NOT_FOUND
```
