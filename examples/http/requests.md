# HTTP 请求样例

所有键、值均为**小写十六进制（hex）**字符串。空键即空字符串 `""`。
以下样例假设服务运行在 `127.0.0.1:8088`，数据目录 `./tabledata`。

常用 hex 对照：

| 原文 | hex |
|---|---|
| （空键） | ``（空串） |
| `a` | `61` |
| `banana` | `62616e616e61` |
| `k01` | `6b3031` |

## 0. 启动

```bash
cargo run --release -- serve --dir ./tabledata --addr 127.0.0.1:8088
```

## 1. 健康检查

```bash
curl -s http://127.0.0.1:8088/healthz
# {"ok":true,"service":"psst"}
```

## 2. 构建一张表（PUT）

请求体为 JSON：`entries` 是 `[key_hex, value_hex]` 数组；
服务端会自动排序，重复键返回 400。`block_size`、`restart_interval` 可选。

```bash
curl -s -X PUT http://127.0.0.1:8088/tables/demo \
  -H 'Content-Type: application/json' \
  -d '{
        "block_size": 256,
        "restart_interval": 4,
        "entries": [
          ["",              "7630"],
          ["00",            "7631"],
          ["61616130",      "7632"],
          ["61616131",      "7633"],
          ["6161613261",    "7634"],
          ["6161613262",    "7635"],
          ["62616e616e61",  "7636"],
          ["7a",            "7637"]
        ]
      }'
```

成功（以下数值为真实运行输出）：

```json
{"ok":true,"table":"demo","bytes":152,"entries":8,"data_blocks":1}
```

> 注意：同一表名重复 PUT 返回 **409 Conflict**（不覆盖已有文件）。
> 要重建请先 `DELETE`。

也可以直接用样例文件：

```bash
curl -s -X PUT http://127.0.0.1:8088/tables/sample \
  -H 'Content-Type: application/json' \
  --data-binary @examples/http/sample_table.json
```

## 3. 列出所有表

```bash
curl -s http://127.0.0.1:8088/tables
# {"ok":true,"tables":["demo","sample"]}
```

## 4. 点查 GET

```bash
# 空键
curl -s 'http://127.0.0.1:8088/tables/demo/get?key='
# {"ok":true,"found":true,"key":"","value":"7630"}

# 查 "banana"
curl -s 'http://127.0.0.1:8088/tables/demo/get?key=62616e616e61'
# {"ok":true,"found":true,"key":"62616e616e61","value":"7636"}

# 未命中
curl -s 'http://127.0.0.1:8088/tables/demo/get?key=ff'
# {"ok":true,"found":false,"key":"ff"}
```

## 5. 范围扫描 SCAN

查询参数：

* `start=<hex>`：默认**含**下界；加 `start_exclusive` 变为不含。
* `end=<hex>`：默认**不含**上界；加 `end_inclusive` 变为含。
* `limit=N`：最多返回 N 条。
* 任一边界省略即为无界。

```bash
# 全表
curl -s 'http://127.0.0.1:8088/tables/demo/scan'

# [aaa0, z)：跨多个数据键区间
curl -s 'http://127.0.0.1:8088/tables/demo/scan?start=61616130&end=7a'
# count=5：aaa0 aaa1 aaa2a aaa2b banana（均 < "z"）

# 含上界
curl -s 'http://127.0.0.1:8088/tables/demo/scan?end=61616131&end_inclusive'
# count=4：空键、00、aaa0、aaa1

# 不含空键
curl -s 'http://127.0.0.1:8088/tables/demo/scan?start=&start_exclusive'

# 限制条数
curl -s 'http://127.0.0.1:8088/tables/demo/scan?limit=3'
```

返回形如：

```json
{"ok":true,"count":5,"entries":[["61616130","7632"], ... ]}
```

## 6. 严格校验 VALIDATE

```bash
curl -s -X POST http://127.0.0.1:8088/tables/demo/validate
```

成功：

```json
{"ok":true,"valid":true,"file_size":152,"total_entries":8,"total_restarts":2,
 "data_blocks":[{"offset":0,"payload_size":71,"entries":8,"restarts":2}]}
```

文件损坏时返回 **400**：

```json
{"ok":false,"error_kind":"corruption",
 "error":"corruption: block checksum mismatch at offset 0 (stored 0x...., computed 0x....)"}
```

## 7. 删除

```bash
curl -s -X DELETE http://127.0.0.1:8088/tables/demo
# {"ok":true,"deleted":"demo"}
```

## 8. 错误样例

```bash
# 非 hex（奇数长度）        -> 400 invalid_argument
curl -s -X PUT http://127.0.0.1:8088/tables/bad \
  -d '{"entries":[["abc",""]]}'

# 重复键（服务端排序后发现）  -> 400 invalid_argument
curl -s -X PUT http://127.0.0.1:8088/tables/bad \
  -d '{"entries":[["6b","31"],["6b","32"]]}'

# 访问不存在的表             -> 404
curl -s 'http://127.0.0.1:8088/tables/nope/get?key=6b'
```
