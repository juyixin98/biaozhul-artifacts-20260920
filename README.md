# quorumcheck — 加权法定人数检查与确定性模拟

纯后端 Go 项目，包含两部分：

1. **加权法定人数（weighted quorum）配置检查器** — 验证读/写法定人数在故障域模型下的相交安全性，枚举各故障场景下的可用节点集合，对不安全配置输出**最小反例**。
2. **确定性离散事件模拟器** — 单进程模拟多个节点上的法定人数读写寄存器；模拟网络可**丢包、重复、乱序**；同一种子输出完全一致。不依赖任何真实集群。

无前端、无外部依赖（仅 Go 标准库）。

## 构建与测试

```bash
go build -o quorumsim ./cmd/quorumsim
go vet ./...
go test ./... -count=1
```

实际运行结果（Go 1.22.2, linux/amd64）：

```
?   	quorumcheck/cmd/quorumsim	[no test files]
ok  	quorumcheck/internal/quorum	0.006s
ok  	quorumcheck/internal/sim	0.003s
```

全部通过，无未通过项。

## CLI 用法

```
quorumsim check <config.json|->   检查读/写与写/写法定人数相交性
quorumsim enum  <config.json|->   枚举每个故障场景的可用集合与最小法定人数
quorumsim run   <run.json|->      运行确定性离散事件模拟
```

输入为文件路径，`-` 表示标准输入。输出为 stdout 上的 JSON。
退出码：`0` = 安全/无违规；`1` = 不安全/发现违规；`2` = 输入或配置错误。

## 配置格式（check / enum）

```json
{
  "nodes": [{"id": "a1", "weight": 1, "domain": "rack-a"}],
  "read_threshold": 4,
  "write_threshold": 4,
  "max_failed_domains": 1
}
```

- `weight`：非负整数。**零权重**合法但产生警告（可加入法定人数但不贡献权重，且绝不会出现在最小法定人数/最小反例中）；负权重报错。
- **重复节点 id** 报错（退出码 2）。
- `domain`：故障域标签；同一域内节点被认为同时故障。空 domain 视为该节点自成一域。
- `max_failed_domains`：枚举所有不超过该数量的故障域子集（含**整域故障**），每个子集是一个故障场景。
- 节点数上限 20（穷举规模限制）。

### 安全性定义

对每个故障场景，令可用节点集合为 S：

- **读写安全**：不存在读法定人数 Qr ⊆ S（权重 ≥ read_threshold）与写法定人数 Qw ⊆ S（权重 ≥ write_threshold）使 Qr ∩ Qw = ∅。
- **写写安全**：不存在两个不相交的写法定人数。
- 阈值为 0 或超过可用权重时该场景无法形成法定人数（活性问题），在 `scenarios[].read_quorum_possible / write_quorum_possible` 中报告；无法形成任何法定人数时视为空虚安全。

注意：经典条件 `R+W > 总权重`（读写）与 `2W > 总权重`（写写）对加权系统是**充分非必要**条件（例如单节点权重 3、阈值 1/1 时不存在不相交法定人数）。本检查器不做闭式近似，直接穷举判定，并以测试中的独立子集对穷举作为参考实现交叉验证（`TestAgainstBruteForce`，300 组随机小规模配置逐场景比对）。

### 最小反例

不安全时输出 `rw_counterexample` / `ww_counterexample`：使 `|QuorumA| + |QuorumB|` 最小（并列时按节点 id 字典序最小）的一对不相交法定人数，含其发生的故障场景。

实际运行 `./quorumsim check examples/check_unsafe.json`（4 节点各权重 1，阈值 2/2，退出码 1）：

```json
"rw_counterexample": {
  "kind": "rw",
  "failed_domains": [],
  "quorum_a": ["n1", "n2"],
  "quorum_b": ["n3", "n4"],
  "weight_a": 2,
  "weight_b": 2
}
```

## 模拟运行格式（run）

```json
{
  "config": { ...同上... },
  "seed": 42,
  "network": {"drop_rate": 0.1, "dup_rate": 0.2, "max_delay": 4},
  "down": ["n3"],
  "ops": [
    {"id": "w1", "type": "write", "at": 0, "version": 1, "value": "x"},
    {"id": "r1", "type": "read", "at": 40, "targets": ["n1", "n2"], "timeout": 50}
  ],
  "trace": true
}
```

- `seed`：同一种子产生逐字节相同的输出（事件按 `(时间, 序号)` 全序处理）。
- `network`：`drop_rate` 丢包概率、`dup_rate` 重复概率、`max_delay` 额外随机延迟上界（造成乱序）。重复应答**不会**重复累计权重（按节点去重）。
- `down`：整场运行不可用的节点（模拟整域/节点故障）。
- `ops[].targets`：客户端只联系的节点子集；缺省为所有存活节点。`timeout` 缺省 50 tick，凑不齐法定人数权重则该操作 `timeout`。
- 写操作携带显式 `version`；节点按 last-writer-wins（版本高者胜）存储。
- 违规检测：若某读操作完成时返回的版本低于"在它开始前已完成的最高版本写"，则记入 `violations`（原子寄存器违规）。

### 实际运行记录

安全配置（`examples/run_safe.json`，3 节点权重 1、阈值 2/2、丢包 0.1/重复 0.2/乱序，退出码 0）：两个写与两个读全部 `ok`，读均返回最高版本 `(v2,"y")`，`violations: null`。

不安全配置（`examples/run_unsafe.json`，4 节点权重 1、阈值 2/2，两个写分别落在不相交法定人数 {n1,n2} 与 {n3,n4}，随后读 {n1,n2}，退出码 1）：

```json
"violations": [
  {
    "read_id": "r1",
    "read_version": 1,
    "read_value": "a",
    "missing_write_id": "w2",
    "missing_version": 2,
    "missing_value": "b",
    "detail": "read r1 returned (v1,\"a\") but write w2 completed at t=2 with (v2,\"b\") before the read started at t=20"
  }
]
```

与 `check` 对同一配置给出的最小反例（{n1,n2} ∩ {n3,n4} = ∅）相互印证。

确定性验证：`./quorumsim run examples/run_safe.json` 连续两次输出 `cmp` 一致。

## 目录结构

```
cmd/quorumsim/main.go        CLI（check / enum / run）
internal/quorum/config.go    配置类型与验证（零权重警告、重复节点/负权重报错）
internal/quorum/check.go     故障场景枚举、可用集合、相交性检查、最小反例
internal/sim/sim.go          确定性离散事件模拟器（丢包/重复/乱序）
internal/quorum/check_test.go 穷举参考交叉验证、零权重、重复节点、整域故障、最小反例
internal/sim/sim_test.go     确定性、违规检测、丢包超时、重复去重、节点下线、乱序
examples/                    请求样例（check_safe / check_unsafe / check_zero_weight / run_safe / run_unsafe）
```

## 测试覆盖与验收对应

| 验收项 | 对应测试 |
|---|---|
| 小规模穷举作为参考 | `TestAgainstBruteForce`（独立子集对穷举，逐场景比对 300 组随机配置） |
| 不安全配置的最小反例 | `TestMinimalCounterexample`（钉死 {n1,n2}/{n3,n4}） |
| 零权重 | `TestZeroWeight`（警告 + 最小法定人数不含零权重节点） |
| 重复节点 | `TestDuplicateNodeRejected`（配置级报错） |
| 整域故障 | `TestWholeDomainFailure`、`TestSafeUnderDomainFailure` |
| 模拟确定性 | `TestDeterminism`（同种子逐字节一致） |
| 丢包/重复/乱序 | `TestDropsCauseTimeout`、`TestDuplicatesDoNotDoubleCount`、`TestReordering` |
| 读写/写写相交违规 | `TestUnsafeConfigViolation`（模拟侧）、`TestAgainstBruteForce`（检查侧） |

## 已知限制

- 检查器为穷举实现，节点数限制为 20（故障场景数 × 2^可用节点 的规模控制）。
- 模拟器是单进程教学级模型：无崩溃恢复、无重配置，写版本由运行脚本显式指定。
