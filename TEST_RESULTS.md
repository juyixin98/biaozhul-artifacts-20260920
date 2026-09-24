# 实测记录（TEST_RESULTS.md）

记录时间：2026-09-24。以下均为本机真实执行结果，未做修饰。

## 环境

- OS：Linux 6.8.0 x86_64；Go：`go version go1.23.4 linux/amd64`
  （位于 `/usr/local/go/bin/go`，不在默认 PATH 中，`demo.sh` 已做兼容）
- 第三方依赖：无；`go vet`、`gofmt` 均干净。

## 单元 + HTTP 测试

命令：`go test -race -count=1 -v ./...`

结果：**PASS**，22 个测试全部通过（含向量时钟比较、兄弟版本剪枝、因果覆写、
部分写超时、读修复、并发冲突 ×2 配置、冲突解决、副本恢复、仲裁不足、
参数校验、HTTP 200/400/503/504、慢副本晚到落地）。

```
ok  github.com/example/quorumlab  1.610s
```

- `-race` 重复运行（`-count=3`、`-count=5`）均无数据竞争、无 flake。
- 覆盖率：`go test -cover` → `coverage: 79.9% of statements`
  （未覆盖部分主要是少量防御性错误分支与 JSON 编码错误路径）。
- `go vet ./...`：无输出（通过）。`gofmt -l .`：无输出。
- `go build ./...`：通过。

## HTTP 状态码实测（手动 curl）

| 场景 | 结果 |
|---|---|
| 部分写：5 副本中 3 个 down，超时 500ms，只收到 2 < W=3 个确认 | **504** |
| 随后读：只有 2 个响应 < R=3 | **503**（body 仍含部分数据） |
| `r=1` 直读单个健康副本 | **200** |
| `/write` 缺 `key` | **400** |

## 端到端演示（`./demo.sh`，N=5 W=3 R=3）

实际运行通过（退出码 0）。关键观测：

1. **基本写/读**：写收到全部 5 个 ack，`quorum=true committed=true`；读
   单版本 `blue`，`conflict=false has_uncommitted=false`。
2. **部分写成功后超时**：副本 3,4 `down`、副本 2 `delay 800ms`，协调者
   `timeout_ms=200` 时只收到 0,1 两个 ack：
   `acked=2, quorum=false, committed=false, timeout=true`，副本 2 在结果中
   记为 `timeout`、3/4 记为 `error`。等待 1s 后再读副本 2，其意向**晚到
   落地**，但版本仍 `committed=false`；R=1 读到的值带
   `has_uncommitted=true`（系统如实标注“未定历史”）。
3. **并发盲写（W+R=6>N=5）**：写 A→{0,1,2}、写 B→{0,3,4}，两者都
   `quorum=true`；读 {1,3,4} 得 `conflict=true`，返回两个向量时钟并发的
   版本（`{w3:1}` 与 `{w4:1}`），证明 W+R>N 不消除并发冲突。
4. **读修复**：写只到 {1,2}（本次写覆盖 `w=2`），从 {1,3} 读且
   `repair=1`，响应中 `repaired_to` 包含 3；随后 `/state` 可见副本 3
   已持有该版本且 `committed=true`。
5. **副本恢复**：副本 4 down 期间错过若干写；`/recover` 后
   `status=recovered`，`keys_pulled` 列出从健康副本反熵补齐的键；
   R=1 直读副本 4 能读到恢复期间错过的 `back=online`。
6. **冲突解决**：`/resolve` 以全部兄弟版本为共同因果后继写入
   `A-then-B-resolved`；全副本读后 `conflict=false`，唯一版本时钟为
   `{w3:1,w4:1,w7:1}`（支配两个原兄弟版本）。

完整逐字输出可用 `./demo.sh` 重新生成（开发时留存于 `/tmp/demo-output.txt`，
属于临时文件，未纳入仓库）。

## 已完成项

- N 副本内存模拟，W/R 可配置（启动参数与 `PUT /config`，带合法性校验）。
- 向量时钟版本、最大版本集剪枝、多值兄弟冲突保留与显式上报。
- 写协调：并行意向、超时、部分成功留痕（uncommitted）、committed 标记
  best-effort 传播。
- 读协调：仲裁判定、冲突/未确认检测、读修复（响应副本或全部健康副本）。
- 故障注入（up/down/delay）、副本恢复反熵、全量状态快照、重置。
- 冲突解决原语（因果后继写，支持指定版本或合并全部）。
- 自动化测试（`-race`）、端到端演示脚本、README（含启动、依赖、样例、
  语义边界与建模取舍）。

## 未完成 / 刻意不做的项

- 不实现真实共识（Raft/Paxos）、领导者租约或线性一致读——本项目的目的
  正是展示“仅 W+R>N 不足”，不是提供可用于生产的 KV。
- 不模拟真实网络（丢包、乱序、重复、分片）；RPC 是进程内方法调用，
  `delay` 仅体现为可注入的固定延迟；晚到意向一定会落地（没有丢弃通道）。
- 无持久化（重启即空）、无鉴权/TLS、无成员变更（运行期改 N 直接重建）。
- `committed` 标记传播是 best-effort（单副本 500ms 上限），极端情况下
  要靠后续读修复最终收敛；未实现提示式 hinted handoff 或向量反熵的后台
  定期扫描（`/recover` 为手动触发）。
- 覆盖率约 80%，个别防御性错误分支未单测。
