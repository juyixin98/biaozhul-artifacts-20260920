# 运行记录（如实）

环境：Ubuntu 24.04.4，x86_64，用户 `admin`（非 root）。日期 2026-09-23。

## 1. 工具链准备

- 机器上没有 Go（`go: command not found`，`/usr/local/go` 不存在）。
- 从 https://go.dev/dl/go1.23.4.linux-amd64.tar.gz 下载到 `~/.local/go`。
  - **第一次下载被截断**（70,553,950 字节，正常应为 73,645,095），解压后 `go build` 报标准库自相矛盾的编译错误：
    ```
    # runtime
    /home/admin/.local/go/src/runtime/traceexp.go:22:6: unsafeTraceExpWriter redeclared in this block
    ... hash64.go: undefined: hashkey ... too many errors
    ```
    删除后改从 https://dl.google.com/go/go1.23.4.linux-amd64.tar.gz 重新下载（73,645,095 字节），`go version` 正常：`go version go1.23.4 linux/amd64`。
- 后续所有命令使用 `export PATH=$HOME/.local/go/bin:$PATH`。

## 2. 开发过程中实际出现过的失败与修复（均已修复并复测）

1. `invalid operation: operator ! not defined on b[t] (map index expression of type struct{})`
   —— 对 `map[Tag]struct{}` 取值当布尔用。改为 comma-ok 写法。
2. GC 单元测试初次失败：`reclaimed 1 tags without all witnesses`
   —— 这是**测试本身写错**：调用 `Reclaim(tag, a, b)` 时漏传了仍持有标签、可能回归的副本 c。修正为用"持有标签但不知道墓碑"的过期见证者来验证拒绝回收。
3. 丢包/乱序场景首轮测试不收敛（seed 2024，n3 收不到 c）；GC 场景、fuzz 场景同样报 `converged=false`。
   —— 原因：35% 丢包下只安排 3 轮 gossip，个别消息连续丢失后无人重传；这是**模拟输入不足**而非 CRDT 错误（全丢时报告正确给出 `converged=false`，见 `TestTotalLossDoesNotFalselyConverge`）。在样例和测试中补足多轮间隔充分的全连接 gossip 后收敛。
4. GC 后三副本不相等：`converged=false`，trace 显示 GC 前状态已一致。
   —— 两个实现缺陷：
   - 协调 GC 逐节点回收，后回收的节点把"已被压缩、墓碑消失"的节点当见证者；
   - `removeTag` 用 `tags[:0]` 原地过滤，与入参切片共享底层数组，破坏遍历。
   
   修复：回收决策统一基于**回收前快照**（原子屏障语义）；`removeTag` 改为分配新数组。
5. CLI 参数 `orset run config.json --trace` 被标准库 `flag` 在位置参数处停止解析而报错
   （`run requires exactly one config file path`）。改为先把 argv 中 flag 与位置参数分离再解析，两种顺序都支持。

## 3. 最终实际执行的命令与结果

### 3.1 静态检查与构建

```
$ go vet ./... && go build ./...
BUILD_OK
$ go build -o bin/orset ./cmd/orset
```
无任何告警/错误。

### 3.2 自动化测试

```
$ go test ./... -count=1
?   orsetsim/cmd/orset [no test files]
ok  orsetsim/internal/orset
ok  orsetsim/internal/sim

$ go test ./... -count=20        # 连跑 20 遍检查确定性/flaky：全部 ok
$ go test -race ./... -count=3   # 竞态检测：全部 ok
```

测试清单：

| 包 | 测试 | 对应验收点 |
|---|---|---|
| orset | `TestAddRemoveLookup` / `TestReAddAfterRemove` | 增加用唯一标签、删除只移除已观察标签 |
| orset | `TestObservedRemoveKeepsConcurrentAdd` | **并发新增保留** |
| orset | `TestAcceptance_ThreeReplicaEnumeration` | **三副本枚举**：6 种合并全序 × 双向 gossip 调度、乱序快照两个到达序、重复同步 100 次、删除再传播 |
| orset | `TestThreeReplicaMergeOrderEnumeration` | 三副本添加/删除 + 合并顺序枚举 |
| orset | `TestMergeAlgebraRandomized`（200 组随机状态） | 交换、结合、幂等 |
| orset | `TestRepeatedSyncIsIdempotent` | **重复同步收敛** |
| orset | `TestReclaimStability` | 墓碑回收稳定性前提（不安全前提必须拒绝） |
| orset | `TestJSONRoundTrip` | JSON 状态编解码确定有序 |
| sim | `TestPerfectNetworkConverges` | 理想网络增删收敛为空集 |
| sim | `TestDeterministicReplay` | 同 seed 报告逐字节相同；不同 seed 轨迹不同 |
| sim | `TestLossyReorderedRepeatedSyncConverges`（9 个 seed） | **丢包+重复+乱序，多轮同步收敛** |
| sim | `TestOutOfOrderMergeEnumeration` | 乱序快照两种到达序均不复活已删元素 |
| sim | `TestSimConcurrentAddPreserved` | 模拟层并发新增保留 |
| sim | `TestFullReorderConverges` / `TestDuplicateDeliveryIdempotent` | 纯乱序收敛 / 纯重复幂等 |
| sim | `TestTotalLossDoesNotFalselyConverge` | 消息全丢时如实报告 `converged=false` |
| sim | `TestSimCoordinatedGC` | 协调 GC 压缩状态且查询不变 |
| sim | `TestFuzzConvergence`（25 个随机场景） | 随机操作 + 坏网络最终一致 |
| sim | `TestValidationErrors` | 非法配置被拒绝 |

### 3.3 四个样例的实际运行结果

```
$ for f in examples/0*.json; do ./bin/orset run -o examples/output/<name>.report.json --trace $f; done

01-three-replica-add-delete   converged=true equal=true final=[b]     ticks=22 counts=add:2 deliver:14 remove:1 send:14
02-concurrent-add-out-of-order converged=true equal=true final=[x]    ticks=34 counts=add:2 deliver:12 remove:1 reorder:11 send:12
03-lossy-repeated-sync        converged=true equal=true final=[a b c] ticks=87 counts=add:3 deliver:22 drop:14 duplicate:6 reorder:11 send:30
04-tombstone-gc               converged=true equal=true final=[]      ticks=30 counts=add:1 deliver:14 gc:1 remove:1 send:14
```

重点解读：

- 样例 01：n1 添加 a，同步后 n2 添加 b，n1 删除 a；最终所有副本 `{b}`，a 的标签留在 adds 中、墓碑在 removed 中。
- 样例 02：n1、n2 在互相听见之前**并发**添加同一元素 x；n1 只观察到自己的标签就删除，乱序率 90%。最终三副本仍为 `{x}`——并发的 `n2:1` 被保留。
- 样例 03：35% 丢包、30% 重复、60% 乱序；30 次发送中 14 次被丢弃、6 次重复、11 次被延后，经 5 轮全连接 gossip 后收敛到 `{a,b,c}`。
- 样例 04：删除传播到全部副本后执行一次协调 GC，三副本的 adds/removed 均被压缩为空，查询结果不变。

完整 JSON 报告与文本 trace 保存在 `examples/output/`。

### 3.4 确定性复核

```
$ ./bin/orset run -o /tmp/r1.json examples/03-lossy-repeated-sync.json
$ ./bin/orset run -o /tmp/r2.json examples/03-lossy-repeated-sync.json
$ diff /tmp/r1.json /tmp/r2.json && echo OK
DETERMINISM: identical reports OK
```
把 seed 改为 5555 后报告不同（轨迹确实随种子变化）。

### 3.5 HTTP JSON 接口

```
$ ./bin/orset serve 127.0.0.1:18080 &
$ curl -s http://127.0.0.1:18080/healthz
ok
$ curl -s -X POST http://127.0.0.1:18080/simulate -H 'Content-Type: application/json' \
    --data @examples/02-concurrent-add-out-of-order.json
{"seed":7,"converged":true,"all_states_equal":true,"final_values":["x"], ...}
```

## 4. 未通过项 / 已知限制（如实）

- 无未通过的测试或验收项；最终 `go test ./...`、20 连跑、`-race` 均通过。
- 已知的模型限制（非失败，属设计边界）：
  1. 模拟器是**单进程离散事件**模型，没有真实网络、真实时钟或磁盘；同步消息传输的是发送时刻的**全量状态快照**。
  2. 收敛的前提是最终有足够的 gossip 把丢包重传过去；若消息 100% 丢失且不再同步，报告会如实给出 `converged=false`（已专门测试）。
  3. 墓碑 GC 只提供"全副本已观察标签与墓碑"的协调回收；分区中、可能带旧状态回归的副本未纳入屏障时**禁止**回收（见 README"墓碑回收的稳定性前提"）。
