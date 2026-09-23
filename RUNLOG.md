# RUNLOG — 实际运行命令与结果记录

环境：Linux 6.8.0-90-generic / amd64，Go 1.22.2。仅使用 Go 标准库
（`math/rand/v2`、`container/heap`、`encoding/json` 等），无外部依赖，
`go.sum` 因此不存在；无真实网络/端口/集群调用。

## 1. 静态检查与构建

```text
$ go vet ./...
（无输出，通过）

$ go build -o /tmp/orset .
（无输出，通过）
```

## 2. 自动化测试

```text
$ go test -race -count=1 ./...
?       orset   [no test files]
ok      orset/accept    1.327s
ok      orset/crdt      1.015s
ok      orset/sim       1.018s
```

测试函数 18 个全部 PASS，0 失败（含 `-race`）。覆盖率：

```text
$ go test -cover ./crdt ./sim ./accept
ok      orset/crdt      0.003s  coverage: 89.7% of statements
ok      orset/sim       0.004s  coverage: 86.5% of statements
ok      orset/accept    0.091s  coverage: 87.2% of statements
```

### 2.1 验收枚举（go test -v 摘要）

```text
--- PASS: TestAcceptanceMatrix (0.06s)
    --- PASS: TestAcceptanceMatrix/A1  合并交换律/结合律/幂等律（300 轮随机状态）
    --- PASS: TestAcceptanceMatrix/A2  三副本乱序合并全排列枚举（两组快照共 726 种，含 6!=720）
    --- PASS: TestAcceptanceMatrix/A3  并发新增在乱序+重复网络下保留（100 种子）
    --- PASS: TestAcceptanceMatrix/A4  观察删除只移除已观察标签，并发新增保留（100 种子+单元断言）
    --- PASS: TestAcceptanceMatrix/A5  重复同步收敛（50 种子，100% 消息复制）
    --- PASS: TestAcceptanceMatrix/A6a 40% 丢包压力下多轮同步最终收敛（30 种子 × 15 轮）
    --- PASS: TestAcceptanceMatrix/A6b 丢包导致分叉、补传后重新收敛（seed=1 复现）
    --- PASS: TestAcceptanceMatrix/A7  墓碑回收稳定性前提（安全路径+复活反例）
    --- PASS: TestAcceptanceMatrix/A8  同场景同 seed 两次运行逐字节一致
PASS
```

## 3. CLI 实际运行

```text
$ /tmp/orset accept -o examples/output/accept-report.json
已写入 examples/output/accept-report.json (1831 字节)
$ echo $?
0
```

四个样例场景（输出文件在 `examples/output/`）：

```text
$ for f in examples/0*.json; do /tmp/orset run -o "examples/output/$(basename $f .json).result.json" "$f"; done
已写入 examples/output/01-basic.result.json (8393 字节)
已写入 examples/output/02-unreliable.result.json (7444 字节)
已写入 examples/output/03-concurrent.result.json (7572 字节)
已写入 examples/output/04-partition-diverge.result.json (1086 字节)
已写入 examples/output/05-heal.result.json (2355 字节)
```

| 场景 | 发送 | 丢弃 | 重复 | 投递 | 冗余合并 | 实际乱序 | 终态（各副本） | converged / matches_expected |
|------|-----:|-----:|-----:|-----:|---------:|---------:|----------------|-------------------------------|
| 01-basic（可靠网络，add+remove+多轮 gossip） | 22 | 0 | 0 | 22 | 16 | 0 | r1/r2/r3 均为 `[x]`（y 被观察删除） | true / true |
| 02-unreliable（30% 丢/重/乱序，3 轮 gossip） | 18 | 6 | 4 | 16 | 10 | 0 | 三副本均为 `[x z]`（y 删除无并发新增） | true / true |
| 03-concurrent（80% 强制乱序，删除 vs 并发新增） | 19 | 0 | 0 | 19 | 14 | 2 | 三副本均为 `[x]`（并发新增 r3#1 存活） | true / true |
| 04-partition-diverge（从不通信，分叉演示） | 0 | 0 | 0 | 0 | 0 | 0 | r1=`[x]`，r2=`[y]` | false / false（预期） |
| 05-heal（先分区各自写入，t=10 双向同步恢复） | 2 | 0 | 0 | 2 | 0 | 0 | r1/r2 均为 `[x y]`（z 被观察删除） | true / true |

03 的乱序在 trace 中可核验（后发消息先于在途消息到达）：

```text
15 r2 -> r3 send_seq=5 (后发先至)
16 r3 -> r1 send_seq=6 (后发先至)
```

确定性复放：

```text
$ /tmp/orset run examples/02-unreliable.json > /tmp/run1.json
$ /tmp/orset run examples/02-unreliable.json > /tmp/run2.json
$ cmp /tmp/run1.json /tmp/run2.json && echo "两次输出逐字节一致"
两次输出逐字节一致
```

04 是故意构造的“无任何同步事件”场景：它演示不通信就不收敛
（CRDT 的保证是**合并后**收敛，不是分区期间一致），因此
`converged=false` 属预期，不是未通过项。

## 4. 开发过程中出现过、已修复的未通过项

以下为首次运行时真实出现的失败，均已修复并有回归测试覆盖：

1. **编译错误 ×2**
   - `crdt/orset.go:137: undefined: t`：`AddWithTag` 内误用不存在的变量名。
     已改为参数 `tag`。
   - `crdt/orset_test.go: undefined: tag`：测试辅助函数只在 accept 包定义。
     已在 crdt 测试包内补 `tag()` 辅助。
2. **测试期望错误 ×1**：`TestMergeCommutativeAssociative` 期望合并结果只有
   `[x]`，但构造的状态中 `y` 从未被删除，正确结果是 `[x y]`。修正期望值，
   x 的并发新增保留断言不变。
3. **验收场景设计错误 ×3（暴露的是场景而非引擎问题，已修正）**
   - A3 只安排单轮 gossip：每个 (from,to) 方向只有一条消息，物理上不可能
     发生乱序，断言 `Reorders>0` 失败。改为 3 轮 gossip 后 100 个种子全部
     观察到乱序并收敛。
   - A5 的拓扑有洞：r3 只从 r2 收过一次消息，r1 的 x 在 100% 重复 +
     乱序下未必到达 r3，出现 `converged=false`。改为两轮全量 gossip
     兜底后 50 个种子全部收敛。
   - A4 中 r1→r2 的 sync 延迟区间（1–6 ticks）宽于 add 到 remove 的间隔
     （2 ticks），r2 可能在尚未观察到 x 时执行删除，语义被稀释。收紧为
     1–2 ticks 并把 remove 推后，保证 r2 确实先观察再删除。
4. **样例 01 的时序问题**：初版让 r2 在 t=4 收到 r1 快照的同一 tick 立刻
   向 r3 推送；事件序上 r2 的发送先于入队的投递，r3 只收到 `{y}`，导致
   最终未收敛。这是状态快照“发送时刻定格”语义的正确表现，样例改为多轮
   gossip 后收敛。该现象也说明 trace 对排查这类时序问题是必要的。

修复后最终状态：`go vet`、`go build`、`go test -race -count=1 ./...`、
`/tmp/orset accept`（退出码 0）全部通过，无遗留未通过项。

## 5. 复现步骤汇总

```bash
go vet ./...
go build -o orset .
go test -race -count=1 ./...
./orset accept
for f in examples/0*.json; do ./orset run "$f"; done
```
