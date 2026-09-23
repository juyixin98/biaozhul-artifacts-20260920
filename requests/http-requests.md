# HTTP 请求样例

服务默认监听 `http://127.0.0.1:8099`。所有 body/字段中的字节均为**小写十六进制**。

启动：

```bash
cargo run --release -- serve --addr 127.0.0.1:8099 --data-dir ./data
```

## 1. 创建/打开仓库

```bash
curl -s -X POST 'http://127.0.0.1:8099/repos/demo/open?chunk_size=16'
```

```json
{
  "repo": "demo",
  "chunk_size": 16,
  "file_length": 0,
  "chunk_count": 0,
  "version": 0,
  "root": "35ef80ba834e9d03ca6d4cf8e22f58a264c7bc34851246977567f7cddf316bb6"
}
```

> 空文件（0 块）也有确定的承诺根 `SHA256("MERKLE_EMPTY_ROOT_v1" ‖ u64-le(16))`，
> 但空文件**不能**生成非空范围证明（没有任何块可包含）。

## 2. 查询状态

```bash
curl -s 'http://127.0.0.1:8099/repos/demo'
```

## 3. 写入/替换文件

body 支持 `{"data":"<hex>"}`（也接受裸 hex 字符串）。写入 50 字节（0..49）：

```bash
HEX=$(python3 -c "print(bytes(range(50)).hex())")
curl -s -X PUT 'http://127.0.0.1:8099/repos/demo/file' \
  -H 'Content-Type: application/json' \
  -d "{\"data\":\"$HEX\"}"
```

```json
{
  "file_length": 50,
  "chunk_count": 4,
  "version": 1,
  "root": "6bd3201f156208c6fa15aed587c7208164be57cd0723d926416592a3db0ee7b7",
  "chunks_written": 4,
  "chunks_deleted": 0
}
```

只改最后一块 1 字节后再次 PUT，返回 `"chunks_written": 1`（仅受影响块参与提交，
Merkle 仅重算脏路径）。

## 4. 范围证明

首块（验证者只读这 1 块，不读完整文件）：

```bash
curl -s 'http://127.0.0.1:8099/repos/demo/proof?start=0&end=1'
```

尾块：

```bash
curl -s 'http://127.0.0.1:8099/repos/demo/proof?start=3&end=4'
```

全范围（省略 end）：

```bash
curl -s 'http://127.0.0.1:8099/repos/demo/proof'
```

响应（`RangeProof`）：

```json
{
  "chunk_size": 16,
  "file_length": 50,
  "total_chunks": 4,
  "start_chunk": 0,
  "end_chunk": 1,
  "root": "6bd3201f156208c6fa15aed587c7208164be57cd0723d926416592a3db0ee7b7",
  "chunks": ["000102030405060708090a0b0c0d0e0f"],
  "steps": [
    { "right": "904b…(32B hex)" },
    { "right": "…", "promoted": false }
  ]
}
```

字段含义见根目录 `README.md` 第 1 节。每步 `left`/`right` 缺省表示该侧无兄弟；
`promoted` 为 `true` 时表示本步切片末尾含整层奇数提升节点。

## 5. 无状态独立验证

把上一步的完整 JSON POST 给 `/verify`（可在任意时刻、任意实例上验证，不依赖仓库）：

```bash
curl -s -X POST 'http://127.0.0.1:8099/verify' \
  -H 'Content-Type: application/json' \
  --data @proof.json
```

成功：

```json
{ "valid": true }
```

失败（原因明确）：

```json
{ "valid": false, "error": "recomputed root does not match committed root" }
```

可能的 `error`：

| 原因 | 触发 |
|---|---|
| `chunk_size must be > 0` | 参数非法 |
| `invalid chunk range` | 空区间 / 越界 |
| `total_chunks inconsistent with file_length/chunk_size` | 伪造文件长度 |
| `chunks length does not match range` | 块数与区间不符 |
| `proof step missing required sibling` | 缺兄弟 / 错位证明 / 步数不符 |
| `recomputed root does not match committed root` | 篡改块、伪造根、位置错配 |

## 6. 一键演示脚本

```bash
bash requests/curl-demo.sh
```

覆盖：空文件根、写入、首/尾/全范围证明与验证、增量只写 1 块，以及篡改内容、
伪造长度、错位、伪造根四类攻击的拒绝结果。
