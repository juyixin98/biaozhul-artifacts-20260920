# 运行记录（RUNLOG）

记录开发与验收过程中**实际执行**的命令与结果。环境：

- Linux 6.8.0-90-generic x86_64，Go `go1.22.2 linux/amd64`
- 日期：2026-09-24

## 1. 静态检查与构建

```text
$ go vet ./...
vet OK

$ go build ./...
（无输出，成功）

$ go build -o /tmp/tracestitch-final ./cmd/tracestitch
build OK: 7647810 bytes
```

## 2. 自动化测试

```text
$ go test -race -count=1 ./...
?   tracestitch/cmd/tracestitch        [no test files]
?   tracestitch/internal/clock         [no test files]
?   tracestitch/internal/model         [no test files]
ok  tracestitch/internal/assembler     1.015s
ok  tracestitch/internal/server        1.030s
ok  tracestitch/internal/storage       1.029s
```

11 个测试函数全部通过（`-race` 竞态检测开启）：

| 包 | 测试 | 验收点 |
|---|---|---|
| assembler | TestOutOfOrderChildBeforeParent | 乱序/子先父后；每版包含关系；时间戳与因果相反也不影响结构 |
| assembler | TestMissingRootTimeoutThenLateCompletion | 缺根超时不完整标志；迟到根 late 修订补全；补全后不再被密封 |
| assembler | TestDuplicateAndConflict | 相同重发幂等无修订；冲突识别差异字段；首条为准 |
| assembler | TestCrossServiceClockSkewDoesNotChangeCausality | ±时钟偏差只告警，树仍按引用挂载，trace 仍 complete |
| assembler | TestParentChainCycles | a→b→c→a 多节点环、d 自环、feeder f 全部标记；健康子树 r/x 不误伤 |
| assembler | TestInvalidSpanRejected | 缺 spanId 拒绝且不建 trace |
| server | TestHTTPEndToEndAssembly | HTTP 全链路：乱序→缺根→冲突→flush 超时→late 补全→包含关系→首条为准→完成后 flush 409 |
| server | TestHTTPErrors | 坏 JSON/空批次/全无效批次/404/非法修订号 |
| server | TestClockSkewOverHTTP | HTTP 层偏差告警 + 引用因果 |
| storage | TestWALReplayRebuildsRevisions | 落盘→关闭→新实例重放，3 条 WAL（span/timeout/span）逐修订一致，快照文件存在 |
| storage | TestOpenEmptyDir | 首次运行空目录 |

## 3. 端到端演示（真实 HTTP）

```text
$ ./examples/demo.sh
（自动挑选空闲端口，临时数据目录，退出清理）
exit=0
```

关键输出节选（字段值来自真实响应）：

- 乱序批量摄入：`checkout-svc` → `initial`(rev1)，`pay-svc` → `extended`(rev2)；
  查询 `missingRoot=true, hasOrphans=true, complete=false`。
- 冲突重发：`status=conflict`，`differingFields=["name","startUnixNano","endUnixNano"]`，
  再查存储的 `pay-svc.name` 仍为首条 `"charge"`。
- 强制超时：`sealed=true`，`latestRevision.reason="timeout", complete=false, missingRoot=true`（rev4）。
- 根迟到：`status=accepted, reason="late"`（rev5）；`GET .../containment` → `"holds": true`；
  最终 forest 为 `gateway → checkout-svc → pay-svc`。
- 时钟偏差：`complete=true`，林根 `api`、子 `inventory`（按引用），
  告警 `kind=child-starts-before-parent, startDeltaNanos=-10000000, toleranceNanos=1000000`。
- 父链循环：`hasCycles=true, cyclePath=["a","b","c","a"]`。
- 持久化产物：`data/wal.jsonl` 14 行事件；`data/snapshots/` 下 4 个 trace 快照。

## 4. 壁钟驱动的真实超时（不用 admin 强制入口）

用短超时启动真实二进制，摄入缺根 trace 后等待：

```text
$ ./tracestitch -trace-timeout 1s -sweep-interval 200ms
摄入后立即查询：{"sealed":false,"latest":"extended"}
等待 >1s：{"sealed":true,"complete":false,"reason":"timeout","missingRoot":true}
服务日志：sweep sealed 1 incomplete trace(s)
```

说明后台 sweeper 的壁钟超时路径同样成立；测试里则用 `clock.Fake` 拨钟，
不 sleep、不以壁钟先后断言因果。

## 5. 崩溃恢复（真实进程重启）

摄入 2 个缺根 span + 一次超时密封后杀进程，WAL 落盘内容：

```text
{"event":"span","seq":1,"span":"worker-1"}
{"event":"span","seq":2,"span":"worker-2"}
{"event":"timeout","seq":3,"span":null}
```

重启二进制：

```text
日志：replayed 3 WAL record(s)
查询：sealed=true, complete=false
      revisions = [
        {v1 initial,  complete=false, missingRoot=true},
        {v2 extended, complete=false, missingRoot=true},
        {v3 timeout,  complete=false, missingRoot=true}
      ]
```

修订版本号、原因、密封状态与重启前一致。

## 6. 过程中发现并修复的问题（如实记录）

1. **环检测漏标 feeder（测试发现的真实缺陷，已修复）**
   初版 `detectCycles` 只在「同一次遍历内」标环；对于 `f → a` 而环 `a-b-c-a`
   已在更早遍历处理完的情况，`f` 未被标记。`TestParentChainCycles` 首次运行失败：
   `node f must be flagged inCycle (cycle member or feeder)`。
   修复：遍历命中 `done` 节点且该节点在环上时，把当前整条 feeder 链也标记。
   修复后测试通过。

2. **demo 脚本 flush 路径写错（脚本问题，已修复）**
   初版 `demo.sh` 把强制超时请求发到 `/v1/traces/{id}/flush`，实际路由注册在
   `/admin/traces/{id}/flush`，返回 `404`、`jq` 解析失败、脚本 `exit=5`。
   已改正两处路径，重跑 `exit=0`。此问题只在演示脚本，不涉及服务端代码。

3. **排查辅助命令的干扰（环境问题，非产品缺陷）**
   手工验证重启时使用 `pkill -f recov/srv`，模式串同时匹配到执行命令的包装
   shell 自身导致命令被中断（退出码 144）；改用按精确进程名 `pkill -x` 后正常。
   与项目代码无关。

## 7. 未通过项 / 已知限制

- 无未通过的自动化测试或验收项（上述问题均已修复并复测通过）。
- 样例范围限制（设计如此，非遗留缺陷）：无鉴权/限流/多副本；快照仅供查看，
  恢复以 WAL 为准；不做时钟同步校准，仅做超容差告警；无前端。
