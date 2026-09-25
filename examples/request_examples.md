启动服务后：

```bash
python -m dms serve --host 127.0.0.1 --port 8080
```

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8080/health
# {"ok": true, "service": "dms", "version": "0.1.0"}
```

## 2. 编译规则

```bash
curl -s -X POST http://127.0.0.1:8080/v1/compile \
  -H 'Content-Type: application/json' \
  --data @examples/request_compile.json
```

## 3. 一步编译 + 脱敏（嵌套数组 / Unicode / 哈希 / drop）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/mask \
  -H 'Content-Type: application/json' \
  --data @examples/request_mask.json
```

响应（节选）：

```json
{
  "ok": true,
  "document": {
    "users": [
      {"name": "张三", "phone": "*******5678",
       "ssn": "274035e77cd66de52fc10a21f5a53713afb832a2fe37aa94e05426587080a7f0"}
    ]
  },
  "stats": {"matched_fields": 3, "rules": [/* 每条规则的 matched/applied */]}
}
```

## 4. 未知规则被默认拒绝（422，响应不含原始值）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/mask \
  -H 'Content-Type: application/json' \
  -d '{"rules":{"version":1,"rules":[{"id":"x","action":"frobnicate","path":"$.a"}]},
       "document":{"a":"TOPSECRET-12345"}}'
```

```json
{"ok": false, "error": {"code": "unknown_rule",
  "message": "未知规则动作 'frobnicate'（默认拒绝）",
  "details": {"action": "frobnicate", "index": 0,
              "supported": ["decrypt", "drop", "encrypt", "hash", "mask", "redact"]}}}
```

## 5. 同优先级作用域冲突（422，fail-closed）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/mask \
  -H 'Content-Type: application/json' \
  -d '{"rules":{"version":1,"rules":[
        {"id":"blanket","action":"redact","path":"$.users[*]"},
        {"id":"spec","action":"mask","path":"$.users[*].secret",
         "options":{"keep_last":0}}]},
       "document":{"users":[{"secret":"TOPSECRET-67890"}]}}'
```

## 6. 显式优先级消解冲突（200）

给上例的两条规则分别加 `"priority": 5` 与 `"priority": 20`：

```json
{"ok": true, "document": {"users": [
  {"name": "***REDACTED***", "secret": "****ef"}]}}
```

## 7. 缺失字段 + require_match（422）

```json
{"ok": false, "error": {"code": "missing_field",
  "message": "规则 m 标记 require_match，但文档中无匹配字段",
  "details": {"rule_id": "m", "path": "$.missing"}}}
```
