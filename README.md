# quorumcheck — 加权法定人数检查器

纯后端工具：验证带整数权重的读/写法定人数配置，**精确穷举**所有法定人数集合，
检测读–写与写–写相交性，枚举故障域（机架、可用区等）整体失效后的可用集合，
并通过一个**确定性离散事件模拟器**在单进程内复现网络丢包、重复、乱序下的
行为。不依赖任何真实集群、网络或 goroutine（模拟完全由事件堆驱动）。

## 它解决什么问题

一个复制系统把读法定人数阈值记为 `R`、写法定人数阈值记为 `W`，节点带整数
权重，节点按故障域分组。要安全，必须满足：

- **写–写相交**：任意两个可能同时成功的写法定人数至少共享一个节点
  （否则两个写可以无共同见证者地同时提交，先后顺序不可判定）；
- **读–写相交**：任意读法定人数与任意写法定人数至少共享一个节点
  （否则读可能完全错过一次已提交的写，返回旧值）；
- **故障域可用性**：在配置允许的任意 `k` 个整域失效后，剩余权重仍能组成
  读、写法定人数。

经典充分条件是 `R + W > totalWeight` 与 `2W > totalWeight`；但权重与故障域
不均匀时，条件本身需要逐集合检验。本工具直接枚举集合给出确定答案，并在不安全
时输出**最小反例**（参与节点最少、总权重最小、字典序兜底）。

## 目录结构

```
cmd/quorumcheck/      CLI：stdin 读 JSON、stdout 写 JSON
internal/quorum/      配置验证 + 掩码穷举分析器（O(n·2^n) 格 DP）
internal/sim/         确定性离散事件模拟器 + 网络策略 + 反例见证构造
examples/             请求样例与实际运行输出（examples/output/）
```

## 构建与测试

需要 Go 1.22+，无第三方依赖。

```bash
go build ./...
go test ./...          # 全部自动化测试
go test -race ./...    # 竞态检测
```

## 使用

```bash
go run ./cmd/quorumcheck [analyze|simulate|all] < request.json
# 或先构建：
go build -o quorumcheck ./cmd/quorumcheck
./quorumcheck analyze < examples/01-safe-majority.json
```

- `analyze`：只做配置验证与穷举分析；
- `simulate`：必须带 `sim` 段，跑离散事件模拟；
- `all`（默认）：分析 + 对每个最小反例自动生成**模拟器见证**（脚本化网络把
  反例一对法定人数真实跑出来），并在给出 `sim` 段时一并模拟。

退出码：`0` 正常（**不安全配置也算正常运行**，结果在 JSON 中）；
`2` 请求非法（配置错误或 JSON 错误，错误在 stdout 的 `errors` 字段）；
`1` 用法或 IO 错误。

## 请求格式

```jsonc
{
  "action": "all",                 // 可省略，也可用命令行参数覆盖
  "config": {
    "nodes": [
      {"id": "n1", "weight": 3, "domain": "az-1"},  // 权重必须是非负整数
      {"id": "n2", "weight": 2, "domain": "az-2"},
      {"id": "observer", "weight": 0, "domain": "az-1"} // 零权重：警告，永不计入
    ],
    "read_quorum": 2,              // 正整数权重阈值
    "write_quorum": 4,
    "tolerate_domains": 1          // 要容忍多少个整域同时失效
  },
  "sim": {                         // 仅 simulate / all 需要
    "seed": 42,                    // 固定种子；同种子逐位可复现
    "runs": 10,                    // 随机模式运行次数（脚本规则强制 1 次）
    "operations": 4,               // 每次运行并发发起的操作数
    "kind": "mixed",               // ww | rw | mixed
    "horizon": 120,                // 虚拟时间上限
    "client_timeout": 10,          // 未凑齐则重发的时间
    "max_attempts": 6,
    "failed_domains": ["az-1"],    // 整域故障：发往这些节点的报文直接消失
    "network": {                   // 随机网络策略
      "loss_rate": 0.2,            // 每条报文独立丢弃概率
      "duplicate_rate": 0.25,      // 复制为两份的概率（客户端按节点去重）
      "min_delay": 1,
      "max_delay": 4,
      "reorder_window": 2          // >0 时部分报文在窗口内提前/推后造成乱序
    },
    "rules": [ /* 或提供规则做确定性脚本化网络，见 examples/05 */ ]
  }
}
```

`rules` 按顺序匹配，第一条命中决定报文命运；字段（`from/to/role/msg_type/op/
min_attempt`）均可省略表示通配。`action` 为 `deliver`（含 `delay`）、
`drop`、`duplicate`（含 `duplicates` 额外副本数）。`role` 为操作在并发对中
的角色 `A`/`B`，整域故障节点的请求带角色 `dead`。

## 输出要点（`report`）

- `valid` / `errors`：配置是否合法（重复节点 id、负权重、阈值超过总权重、
  节点数超过穷举上限 20 等）；
- `warnings`：零权重节点、缺失故障域等非致命问题；
- `num_read_quorums` / `num_write_quorums`：法定人数集合**精确数量**，
  `read_quorums` / `write_quorums` 为按（节点数、权重、掩码）排序的前若干个；
- `ww_safe` / `rw_safe`：相交性结论；不安全时有
  `minimal_ww_counterexample` / `minimal_rw_counterexample`，给出两个不相交
  集合、各自权重与文字解释，`all` 动作还在 `witness` 里附上能在模拟器中复现
  违例的脚本化输入与运行结果；
- `available` / `minimal_availability_failure`：允许的整域故障下是否始终
  可用，以及最小的不可用故障组合；`availability_witness` 用“完美网络 +
  这些域宕机”的脚本运行证明不可用来自故障而非丢包；
- `scenarios`：无故障场景、不可用场景（优先）、部分健康场景，外加一个
  `beyond_tolerance: true` 的“全域宕机”参考场景。

## 算法说明

- 节点子集编码为位掩码（最多 20 个节点，2^20 个集合），一次递推求出每个
  子集的总权重；
- “两个不相交法定人数是否存在”用子集格上的最小传播 DP（SOS DP 风格）在
  O(n·2^n) 内精确求解，避免 O(4^n) 两两比较；
- 对每个掩码 A，在其补集的 `bestW` 表中 O(1) 取出最小的法定人数 B，遍历
  全体 A 即得全局最小不相交对；
- 故障域组合只在权重和层面做可用性判定（O(k)），指数级的集合枚举只对进入
  报告的有限场景运行；
- 模拟器是整数虚拟时钟 + 最小堆事件循环；随机源为内置 splitmix64，
  无系统熵，同种子同输入逐位复现；
- 测试包内另含一个朴素 O(4^n) 暴力参考实现，对结构化用例与数千个随机
  权重/阈值组合做交叉校验（见 `internal/quorum/masks_test.go`）。

## 边界与限制

- 穷举上限 20 个节点（内存约每表 4 MiB）；域故障组合数有上限（200000），
  超出会产生 `scenario_enumeration_truncated` 警告且可用性结论保守；
- 模拟器验证的是“协议可到达的交错”，随机性只抽样不覆盖全部调度；安全性的
  权威结论来自穷举分析器，模拟器用于解释与见证；
- 节点是无状态应答者：模拟的是法定人数交集本身，不包含版本向量/MVCC 等
  上层机制。

## 请求样例

| 文件 | 场景 |
|---|---|
| `examples/01-safe-majority.json` | 3 节点多数派 + 一个零权重观察者（警告） |
| `examples/02-rw-unsafe.json` | 权重 3/2/1，R=2 W=4：读写不相交，且丢失 az-1 后不可用 |
| `examples/03-domain-outage.json` | 5 节点 3 域：容忍单域失效的枚举与参考场景 |
| `examples/04-lossy-network.json` | 安全多数派在 20% 丢包+25% 重复+乱序下：全部完成、0 违例 |
| `examples/05-scripted-split.json` | 脚本化“脑裂”：两个写分别只听到不同单节点 |
| `examples/06-invalid-request.json` | 非法请求（重复 id、负权重、阈值超总权重），退出码 2 |
| `examples/07-lossy-split.json` | R=W=1 的不安全配置在 50% 丢包下：12/12 运行出现违例 |

`examples/output/` 保存了这些请求在本机的实际 JSON 输出。

## 运行记录（实测）

环境：Linux x86_64，Go 1.22.2，无第三方依赖。

```
$ go vet ./...                      # 通过
$ go test -race -count=1 ./...      # 三个包全部 ok
ok  	quorumcheck/cmd/quorumcheck
ok  	quorumcheck/internal/quorum
ok  	quorumcheck/internal/sim
$ go test -cover ./...              # 覆盖率 75% / 92.3% / 93.0%
```

- 20 节点（2^20 集合、10 个故障域）完整穷举：约 0.09 s，法定人数集合
  精确计数 214957（列表截断为前 64 个，计数不截断）。
- 确定性：同一带损网络请求连续运行两次，输出 JSON 逐字节相同（splitmix64
  固定种子）。
- 交叉校验：DP 结论与朴素 O(4^n) 暴力实现，在结构化用例与 4000 组随机
  权重/阈值上完全一致；并验证 `R+W>W_total`、`2W>W_total` 经典定理及其
  等号可分性。

**开发中发现并修复的一个缺陷（如实记录）**：初版模拟器在重传时清空了
“已响应节点集合”却没有同时清空累计权重，导致同一节点在两次尝试中各应答
一次时权重被重复累计——单个权重 1 的节点可能把阈值 2 的操作“凑满”。
交叉测试中表现为：安全的 2/3 多数派配置在有损网络下被报告了不可能存在的
违例（任意两个 ≥2/3 集合必相交）。修复为客户端跨尝试按节点全局去重、权重
即该去重集合之和，并新增回归测试 `TestMajorityNeverViolatesUnderLoss`（60
次高丢包运行零违例）与 `TestCrossAttemptNodeDedup`（单节点多次应答永不
凑齐法定人数）。修复后示例 04 为 40/40 完成、0 违例。

