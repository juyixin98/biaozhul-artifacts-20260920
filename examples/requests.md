# 请求样例

先启动服务：

```bash
python3 server.py --port 8000
```

## 1. 一次性全量解析

```bash
curl -s -X POST http://127.0.0.1:8000/parse \
  -H 'Content-Type: application/json' \
  -d '{"text": "let x = 1 + 2 * 3;"}'
```

响应（节选）：`tree.kind == "program"`，唯一子节点为 `let`，其值表达式是
`binary(+)`，右子树为 `binary(*)`（体现优先级）；`diagnostics: []`，
`stats: {"reused": 0, "parsed": 1}`。

## 2. 带语法错误的解析（错误位置）

```bash
curl -s -X POST http://127.0.0.1:8000/parse \
  -H 'Content-Type: application/json' \
  -d '{"text": "let x = (1 + 2;"}'
```

响应 `diagnostics` 包含：

```json
[{"message": "unclosed '('", "start": 8, "end": 9,
  "start_line": 0, "start_col": 8, "end_line": 0, "end_col": 9}]
```

## 3. 创建文档

```bash
curl -s -X POST http://127.0.0.1:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"text": "let a = 1;\nfn f(x) = x * 2;\nlet b = f(a);\nlet s = \"(not code)\";"}'
```

响应：`{"id": "<doc_id>", "text": ..., "tree": ..., "diagnostics": [], "stats": {...}}`
（HTTP 201）。后续把 `<doc_id>` 代入下面的 URL。

## 4. 增量编辑

把第一个声明里的 `1` 改成 `10`（偏移 8 处删 1 字符插入 `10`）：

```bash
curl -s -X POST http://127.0.0.1:8000/documents/<doc_id>/edit \
  -H 'Content-Type: application/json' \
  -d '{"start": 8, "delete": 1, "insert": "10"}'
```

响应 `stats: {"reused": 3, "parsed": 1}` —— 只有被编辑的声明重解析，
其余 3 个声明节点复用；后续声明的 span 已平移。

制造一个未闭合括号（删除 `fn f(x)` 的 `(`，偏移 16）：

```bash
curl -s -X POST http://127.0.0.1:8000/documents/<doc_id>/edit \
  -H 'Content-Type: application/json' \
  -d '{"start": 16, "delete": 1, "insert": ""}'
```

响应 `diagnostics` 含 `expected '('`（位置 17）等；再把它加回：

```bash
curl -s -X POST http://127.0.0.1:8000/documents/<doc_id>/edit \
  -H 'Content-Type: application/json' \
  -d '{"start": 16, "delete": 0, "insert": "("}'
```

诊断清空。

## 5. 查询当前文档

```bash
curl -s http://127.0.0.1:8000/documents/<doc_id>
```

## 6. 错误请求

```bash
# 越界编辑 -> HTTP 400 {"error": "edit range [999, 999) is outside document of length 65"}
curl -s -X POST http://127.0.0.1:8000/documents/<doc_id>/edit \
  -H 'Content-Type: application/json' \
  -d '{"start": 999, "delete": 0, "insert": "x"}'

# 不存在的文档 -> HTTP 404
curl -s http://127.0.0.1:8000/documents/nope
```
