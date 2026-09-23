# 请求样例（预期响应）

启动：

```bash
./target/release/mvcc-server --dir /tmp/mvcc-demo --addr 127.0.0.1:18080
BASE=http://127.0.0.1:18080
```

以下顺序执行即 `examples/demo.sh` 的内容，附每步预期。

## 1) 初始提交 v1

```bash
curl -s -X POST $BASE/tx/write -d '{}'
# {"write_txn_id":1,"snapshot_version":0}

curl -s -X PUT $BASE/tx/write/1 \
  -d '{"ops":[{"put":{"k":"x","v":"v1"}},{"put":{"k":"y","v":"y1"}}]}'
# {"buffered":2,"write_txn_id":1}

curl -s -X POST $BASE/tx/write/1/commit -d '{}'
# {"committed":true,"version":1}
```

## 2) 长读者钉在 v1

```bash
curl -s -X POST $BASE/tx/read -d '{}'
# {"read_txn_id":1,"version":1}

curl -s "$BASE/tx/read/1/get?key=x"
# {"found":true,"key":"x","value":"v1"}
```

## 3) 两个写者同一快照、同改一键 → 后提交者 409

```bash
curl -s -X POST $BASE/tx/write -d '{}'   # id 2
curl -s -X POST $BASE/tx/write -d '{}'   # id 3
curl -s -X PUT $BASE/tx/write/2 -d '{"ops":[{"put":{"k":"x","v":"A"}}]}'
curl -s -X PUT $BASE/tx/write/3 -d '{"ops":[{"put":{"k":"x","v":"B"}}]}'

curl -s -X POST $BASE/tx/write/2/commit -d '{}'
# 200 {"committed":true,"version":2}

curl -s -i -X POST $BASE/tx/write/3/commit -d '{}'
# HTTP/1.1 409 Conflict
# {"committed":false,"error":"write-write conflict","keys":["x"]}
```

## 4) 读一致性：最新值是 A，长读者仍是 v1

```bash
curl -s "$BASE/get?key=x"
# {"found":true,"key":"x","value":"A"}

curl -s "$BASE/tx/read/1/get?key=x"
# {"found":true,"key":"x","value":"v1"}

curl -s "$BASE/tx/read/1/get?key=y"
# {"found":true,"key":"y","value":"y1"}
```

## 5) 活跃快照期间：水位线=1，没有可回收字节

```bash
curl -s "$BASE/stats"
# 含 "watermark":1,"snapshots":[{"id":1,"version":1}],"reclaimable_bytes":0

curl -s -X POST $BASE/gc -d '{}'
# {"ran":false,"reason":"nothing to reclaim at current watermark"}

curl -s "$BASE/tx/read/1/get?key=x"
# 仍是 "value":"v1" —— 快照需要的版本没被删
```

（在继续之前多提交若干次，制造历史版本。）

## 6) 释放快照 → 水位线上移 → GC 真正回收

```bash
curl -s -X POST $BASE/tx/read/1/release -d '{}'
# {"released":true,"read_txn_id":1}

curl -s "$BASE/stats"
# "watermark":8,"snapshots":[], "reclaimable_bytes":325

curl -s -X POST $BASE/gc -d '{}'
# {"ran":true,"watermark":8,"bytes_before":422,"bytes_after":97,"reclaimed":325}

curl -s "$BASE/get?key=x"
# 最新值不受影响

curl -s "$BASE/stats"
# committed_versions 从 7 降为 2（每个存活键一条基线）
```

## 7) 其它

```bash
# 放弃事务
curl -s -X POST $BASE/tx/write -d '{}'                 # id N
curl -s -X POST $BASE/tx/write/N/abort -d '{}'        # {"aborted":true}
curl -s -X POST $BASE/tx/write/N/commit -d '{}'       # 404 unknown write txn

# 坏请求体
curl -s -i -X PUT $BASE/tx/write/1 -d 'not json'      # 400
curl -s -i "$BASE/nope"                               # 404
```
