# 验收运行记录 (RUN REPORT)

记录时间（UTC）：2026-09-23T19:27:50Z

本文件如实记录在本机实际执行的命令、结果与未通过项。无前端、无云连接。

## 环境

```
go version go1.22.2 linux/amd64
Linux 6.8.0-90-generic x86_64
jq: jq-1.7, bash: GNU bash, version 5.2.21(1)-release (x86_64-pc-linux-gnu)
```

## 1. 构建 / 静态检查 / 单元测试

```
$ go build ./...            # 无输出 = 成功，无第三方依赖
$ go vet ./...              # 无输出 = 通过
$ gofmt -l .                # 无输出 = 已格式化
$ go test -race -count=1 ./...
?   	trmerge/cmd/trmerge	[no test files]
ok  	trmerge	1.652s
```

单元/集成测试函数数（不含验收脚本）：**31** 个，全部通过。
语句覆盖率：coverage: 73.2%
coverage: 0.0%。

## 2. 端到端验收（真实起服务 + 真实夹具进程）

```
$ bash scripts/acceptance.sh
```

脚本真实执行：编译二进制 → 启动 HTTP 服务（cache/work 分目录）→ 通过 JSON 接口
投递乱序/重复/迟到事件 → execute 模式真实运行 examples/fixtures 下的 bash 夹具
（含退出码 137 的崩溃夹具）→ 杀掉进程后重启，从 events.log 恢复并比对汇总。

结果：**PASS=27 FAIL=0**（完整原始输出见 docs/acceptance-output.txt）。覆盖断言：

- 事件乱序到达、同 event_id 重投递被标记 duplicate；
- finalize 后到达的通过结果标记 late 且被拒绝，缺失结果仍为 incomplete；
- 尝试ID/测试ID分离：崩溃后 attempt#2 重试通过，测试最终 passed；
- passed/failed/canceled/incomplete 四态计数正确（4/0/1/1）；
- 夹具非零退出被真实记录为 crashed、exit_code=137，在飞尝试为 incomplete；
- 汇总跨重排（/v1/replay shuffle ×5）完全一致；
- 进程重启后从事件日志重建的汇总与重启前逐字节一致；
- 夹具进程的工作目录与缓存目录不同、互不嵌套，缓存内无夹具脚本。

## 3. 已知边界 / 非目标

- 纯后端：无任何 Web UI / 前端资源。
- 单机、本地、无云平台连接、无外部网络依赖、无第三方 Go module。
- execute 模式会以服务进程身份运行**请求中显式给出**的命令；不提供沙箱，
  仅做 work/cache 路径隔离与信号/超时管理，故应只传入可信夹具。
- 汇总为读时全量计算（map 规模为测试数），面向本地 CI 规模，未做分页/增量物化。

## 4. 复现方式

```
# go version go1.22.2 linux/amd64
go build ./...
go test ./...
bash scripts/acceptance.sh
```
