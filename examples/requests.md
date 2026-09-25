# 请求样例（curl）

假设服务运行在 `http://127.0.0.1:8080`：

```sh
go run ./cmd/cacheserver -addr 127.0.0.1:8080 -cache-dir ./cache-data -work-dir ./work-tmp
B=http://127.0.0.1:8080
```

## 健康检查

```sh
curl -s $B/v1/health
# {"status":"ok"}
```

## 上传对象（地址即摘要）

```sh
printf 'hello local cache\n' > blob.txt
D=$(sha256sum blob.txt | cut -d' ' -f1)
curl -s -w '\nHTTP %{http_code}\n' -X PUT --data-binary @blob.txt $B/v1/cas/$D
# {"digest":"f249e836…","size":18,"dedup":false}
# HTTP 201

# 重复上传 → 去重
curl -s -X PUT --data-binary @blob.txt $B/v1/cas/$D
# {"digest":"f249e836…","size":18,"dedup":true}   (HTTP 200)
```

## 下载 / 存在性 / 缺失

```sh
curl -s $B/v1/cas/$D -o got.txt && cmp blob.txt got.txt && echo identical
curl -sI $B/v1/cas/$D | head -1                      # HTTP/1.1 200 OK
curl -s -w '\nHTTP %{http_code}\n' $B/v1/cas/$(sha256sum /dev/null | cut -d' ' -f1)
# {"error":"cas: object not found"}  HTTP 404
```

## 大小超限与摘要不匹配

```sh
head -c 2000000 /dev/urandom > big.bin   # 服务以 -max-object-size 1048576 启动
curl -s -w '\nHTTP %{http_code}\n' -X PUT --data-binary @big.bin \
    $B/v1/cas/$(sha256sum big.bin | cut -d' ' -f1)
# {"error":"cas: object exceeds maximum size: limit is 1048576 bytes"}  HTTP 413

curl -s -w '\nHTTP %{http_code}\n' -X PUT --data-binary @blob.txt \
    $B/v1/cas/$(sha256sum /dev/null | cut -d' ' -f1)
# {"error":"cas: content does not match claimed digest: got f249e836…"}  HTTP 400
```

## 运行显式夹具命令（构建）

```sh
curl -s -X POST -H 'Content-Type: application/json' $B/v1/builds -d "{
  \"argv\": [\"sh\", \"-c\", \"tr a-z A-Z < in.txt > out.txt\"],
  \"inputs\": {\"in.txt\": \"$D\"},
  \"outputs\": [\"out.txt\"],
  \"timeout_ms\": 5000
}"
# {"action_digest":"679398e0…","cache_hit":false,"exit_code":0,
#  "outputs":{"out.txt":"aaad2e09…"},"duration_ms":1}

# 相同请求再次提交 → 命中动作缓存，命令不重复执行
# {"action_digest":"679398e0…","cache_hit":true, ...}
```

## 统计与一致性诊断

```sh
curl -s $B/v1/admin/stats
# {"objects":2,"bytes":36,"ac_entries":1,"max_object_size":1048576}

curl -s $B/v1/admin/fsck
# 健康: {"objects_checked":2,"bytes_checked":36,"corrupt":null,
#        "leftover_tmp":null,"unknown":null,"ok":true}
# 损坏: {"objects_checked":2, ... ,"corrupt":["<digest> (content hashes to …)"],
#        "ok":false}
```
