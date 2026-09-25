# 请求样例

本目录保存可直接用 curl 重放的请求体，以及一次真实运行抓取的响应。

请求文件（`requests/`，均配合 `POST /search` 或 `POST /documents`）：

| 文件 | 演示点 |
|---|---|
| `search_and_order.json` | `pet AND cat AND zebra`：倒排链大小差序，观察交集重排（朴素 12 次探测 → 优化 1 次） |
| `search_not.json` | 纯 `NOT cat`：相对固定文档全集取差集 |
| `search_parens.json` | `(cat OR dog) AND quantum`：括号改变结合方式 |
| `search_mixed.json` | AND/OR/NOT/括号混合 |
| `search_unknown_term.json` | `cat AND unknownword`：未知词倒排链为空 |
| `search_parse_error.json` | `(cat AND dog`：缺右括号，返回错误位置 12 |
| `add_document.json` | POST /documents 的请求体 |

响应文件（`responses/`）：在 2026-09-24 的一次真实运行中，按
"先只读、后增删"的顺序抓取，因此彼此自洽：

- `health.json`、`stats.json` 及 5 个 `search_*.json`（成功/错误）对应**初始 12 篇**语料；
- 随后执行 `add_document.json`（新增 id=13）与 `DELETE /documents/11`，
  于是 `add_document.json`、`delete_doc11.json`、`search_not_after_delete.json`、
  `stats_after_mutations.json` 对应"存活 12 篇（13 篇曾装入、删除 1 篇）"的状态，
  演示删除文档后 NOT 全集收缩（`NOT cat` 由 6 篇变 5 篇）。

完整命令交互见 `../docs/http-demo-2026-09-24.txt`（该记录是另一台全新启动的服务，
未新增文档，删除 11 后存活 11 篇，与本目录的增删会话数字不同，属正常）。

重放示例：

```bash
./run.sh --port 8080
curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  --data @samples/requests/search_and_order.json
```
