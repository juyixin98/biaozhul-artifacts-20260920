# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic (x86_64)，Go 1.22.2，仅标准库，无第三方依赖。
记录日期：2026-09-25。

## 1. 静态检查

```text
$ gofmt -l .
（无输出）

$ go vet ./...
exit=0
```

`gosec`：本机未安装；尝试 `go run github.com/securego/gosec/v2/cmd/gosec@latest`
被会话的自动权限策略拒绝（下载并执行外部代码需用户明确授权），**此项跳过，
未实际运行**。代码不涉及鉴权、支付、SQL、模板渲染；外部输入仅以字节流处理，
HTTP 监听默认绑定 `127.0.0.1`，POST 体限制 16 MiB。

## 2. 自动化测试（竞态检测 + 覆盖率）

命令：

```bash
go test -race -count=1 -timeout 120s -coverprofile=cover.out ./...
go tool cover -func=cover.out | tail -1
```

实际结果（全部 PASS，`-race` 未报告数据竞争）：

```text
ok  github.com/example/rangeserver/cmd/rangeserver   1.079s  coverage: 37.5%
ok  github.com/example/rangeserver/cmd/verify        1.120s  coverage: 67.9%
ok  github.com/example/rangeserver/internal/artifact 1.059s  coverage: 82.1%
ok  github.com/example/rangeserver/internal/blobstore 1.046s coverage: 92.3%
ok  github.com/example/rangeserver/internal/client   1.190s  coverage: 86.3%
ok  github.com/example/rangeserver/internal/clock    1.065s  coverage: 97.8%
ok  github.com/example/rangeserver/internal/fault    1.031s  coverage: 95.7%
ok  github.com/example/rangeserver/internal/rangespec 1.031s coverage: 93.8%
ok  github.com/example/rangeserver/internal/server   1.066s  coverage: 86.1%
total: 82.4% of statements
```

所有内部库（`internal/...`）覆盖率均 ≥ 82.1%，总语句覆盖率 82.4%，满足 80% 下限。
两个 `cmd` 入口偏低（37.5% / 67.9%）：未覆盖部分是 `main()` 中的进程生命周期
（信号监听、优雅关闭、JSON 编码落盘），属于薄装配层；其调用的全部逻辑均在
`internal` 包中并已被测试。`cmd/verify` 的全部 14 个验收场景另有测试
（`cmd/verify/verify_test.go`）直接断言通过。

开发过程中出现并已修复的问题（如实记录）：

1. 初次跑 `-race` 时测试挂起：`FlakyStore` 用例注入了 FakeClock 延迟却没有
   推进时钟，Sleep 永久等待。改为该用例不注入延迟（延迟取消由独立用例覆盖）。
2. `Accept-Encoding: *` 协商实现未真正回退到通配 q 值，已修复并修正用例
   （平局时 identity 优先是项目既定策略）。
3. 一个 gzip 客户端测试断言方向写反（编码后 41 字节 < 原始 704 字节才是正确），
   已修正。
4. curl 演示脚本用 `awk '{print $2}'` 解析 `Content-Range` 只取到 "bytes"，
   已改为输出 `$2" "$3`。

## 3. 验收命令（进程内，结构化 JSON）

```bash
$ go run ./cmd/verify
exit=0
```

结果摘要（完整结构化报告见 `acceptance-report.json`，由 `/tmp/verify` 二进制生成）：

```text
total: 14 passed: 14 failed: 0 all_passed: True
PASS prefix-range
PASS suffix-range
PASS open-ended-range
PASS multi-range-tiled
PASS overlapping-ranges
PASS unsatisfiable-range
PASS zero-length-artifact
PASS etag-mismatch-if-range
PASS if-range-date-forms
PASS malformed-range-ignored
PASS too-many-ranges
PASS retry-after-transport-faults
PASS retry-after-truncated-body
PASS gzip-selected-representation
```

其中：

- 不满足范围 → 416 + `Content-Range: bytes */256`；
- 零长度制品 → 范围请求 416 + `bytes */0`，普通 GET 200 空体；
- 重叠范围（`0-119` 与 `100-255`）→ 服务端合并为单个 `bytes 0-255/256`，
  客户端报告 `overlapping_ranges_requested: true` 且 `verified: true`；
- ETag 不匹配（`If-Range: "stale"`）→ 200 完整 256 字节，
  `X-Range-Decision: ignored-if-range`；
- 范围数量 6 > 上限 5 → 400；
- 两次注入传输错误 / 一次响应体截断后，客户端用 FakeClock 退避重试成功，
  报告里 `attempts` 逐条记录错误与最终 206；
- 每个 client 场景均以 SHA-256 核验重组后的原始字节（`verified: true`）。

## 4. 真实 HTTP 服务 + curl 端到端

注：本机 8080 端口被一个无关 Java 进程占用，因此服务实际在 **18080** 启动；
README/样例中的 8080 是默认端口，用 `-addr` 覆盖即可。

```bash
$ go build -o /tmp/rangeserver ./cmd/rangeserver
$ /tmp/rangeserver -addr 127.0.0.1:18080 &
$ BASE=http://127.0.0.1:18080 bash examples/curl-examples.sh
ALL CURL CHECKS PASSED   (exit=0)
```

脚本共 21 项检查（基础 GET/目录/404、三种范围形式、越界夹紧、multipart、
重叠合并、416、零长度制品、非法 Range、范围上限、If-Range ETag/两种日期、
gzip 表示与 gzip 上的范围、406，以及两处 Content-Range 头值），全部 PASS。

## 5. 独立第三方交叉核验（Python，不依赖 Go 客户端）

用 Python 标准库 `http.client` 直接对运行中的服务下载并手工解析 multipart：

```text
multipart 3-part reassembly: OK, parts=3, sha256= 40aff2e9d2d8922e
overlapping coalesced single 206: OK
If-Range current ETag 206: OK
suffix bytes equal full[-64:]: OK
exit=0
```

三范围 multipart 重组结果与无 Range 的完整 GET **逐字节相等**，SHA-256 一致；
后缀范围字节等于完整表示的最后 64 字节。该核验与 Go 客户端相互独立。

## 6. 未通过项 / 已知限制

- 无失败的测试或验收场景。
- 已跳过（如实说明）：`gosec` 未运行（未安装 + 下载外部代码被权限策略拒绝）。
- 设计内限制，非缺陷：
  - gzip 选定表示上的字节范围按**编码后字节**计数（RFC 语义），gzip 客户端
    下载不携带 Range（整表示取回后 gunzip 再核验）；
  - 服务默认范围数量上限 5（`server.WithMaxRanges` 可调），超限回 400；
  - 服务仅为本机实验用途：内存态、绑定 loopback、无持久化、无鉴权。
