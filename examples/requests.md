# 请求样例

启动服务（缓存目录与源码树分离）：

```sh
go build -o bin/depscan ./cmd/depscan
./bin/depscan -addr 127.0.0.1:8080 -cache-dir /tmp/depscan-cache
```

以下样例针对 `examples/demo-project/`（含同名头 `util.h`×3、嵌套路径、
注释伪 include、`svc/a.h`↔`svc/b.h` 循环引用）。请求体 JSON 已存于
`examples/requests/`，可直接 `-d @文件` 使用。

## 1. 健康检查

```sh
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

## 2. 全量扫描

```sh
curl -s -X POST http://127.0.0.1:8080/v1/scan \
  -H 'Content-Type: application/json' \
  -d @examples/requests/full-scan.json
```

要点：`main.c` 的 `"util.h"` → 根 `util.h`，`<util.h>` → `include/util.h`；
`net/open.c` 的 `"util.h"` → `net/util.h`（同名头按搜索路径区分）；
`cycles` 报告 `[["svc/a.h","svc/b.h"]]`；注释伪 include 不出现在结果中。

## 3. 增量：文件修改

```sh
curl -s -X POST http://127.0.0.1:8080/v1/scan/incremental \
  -H 'Content-Type: application/json' \
  -d @examples/requests/incremental-changed.json
```

`changed: ["util.h"]` → `affectedFiles` 为反向闭包
`[main.c, net/open.c, net/runner.h, util.h]`，目标 `app`、`net` 均受影响；
同名的 `include/util.h` 不受影响。

## 4. 增量：文件删除

```sh
rm examples/demo-project/svc/b.h   # 真实删除文件
curl -s -X POST http://127.0.0.1:8080/v1/scan/incremental \
  -H 'Content-Type: application/json' \
  -d @examples/requests/incremental-deleted.json
```

`svc/b.h` 节点与所有入边被移除；受影响集合基于删除前的旧图计算，
`svc/a.h`、`net/runner.h`、`main.c`、`net/open.c` 均被标记。

## 5. 读取缓存的依赖图

```sh
curl -s "http://127.0.0.1:8080/v1/graph?sourceRoot=$PWD/examples/demo-project"
```

## 6. 错误场景

```sh
# 宏生成 include -> 422
mkdir -p /tmp/macro-demo && printf '#include DYNAMIC_MACRO\n' > /tmp/macro-demo/x.c
curl -s -X POST http://127.0.0.1:8080/v1/scan \
  -H 'Content-Type: application/json' -d '{"sourceRoot":"/tmp/macro-demo"}'

# 缓存目录与源码根未分离 -> 400
curl -s -X POST http://127.0.0.1:8080/v1/scan \
  -H 'Content-Type: application/json' -d '{"sourceRoot":"/tmp/depscan-cache"}'

# 未全量扫描就增量 -> 409
curl -s -X POST http://127.0.0.1:8080/v1/scan/incremental \
  -H 'Content-Type: application/json' -d '{"sourceRoot":"/tmp/fresh-dir","changed":["a.c"]}'
```
