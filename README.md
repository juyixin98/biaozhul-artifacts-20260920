# 优先级继承调度模拟器 (Priority Inheritance Scheduling Simulator)

一个**确定性、离散时间**的实时调度模拟器，用于演示和验证**优先级继承协议
（Priority Inheritance Protocol, PIP, Sha–Rajkumar–Lehoczky 1990）**：

- 任务具有固定**基优先级**、**释放时间**和由 `compute / lock / unlock` 组成的固定执行序列；
- 单 CPU、固定优先级抢占式调度；
- 任务在互斥锁上阻塞时，锁持有者**继承**当前最高优先级阻塞者的有效优先级；
- 支持**传递继承（嵌套锁链）**：持有者自己也被阻塞时，捐赠沿等待图传递到固定点；
- 锁释放后选择最高优先级等待者直接交接（priority-ordered direct handoff），
  持有者优先级**正确恢复**；
- 内置死锁环检测（PIP 能限制优先级反转，但**不能**防止死锁）。

纯后端服务：Go 标准库 `net/http` + JSON，无任何界面、无第三方依赖。

---

## 1. 依赖与启动

- **Go 版本**：Go 1.23（仅用标准库；`go.mod` 即为依赖锁定文件，`go.sum` 无需第三方条目）。
- **外部依赖**：无。

```bash
# 在项目根目录（包含 go.mod 的目录）
go run .                    # 默认监听 :8080
PORT=8097 go run .          # 自定义端口
ADDR=127.0.0.1:9000 go run . # 或用 ADDR 指定完整地址
```

构建为二进制：

```bash
go build -o pip-sim .
PORT=8080 ./pip-sim
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
|----|----|----|
| GET | `/` | 服务信息与接口清单 |
| GET | `/healthz` | 存活探针 |
| GET | `/api/scenarios` | 列出内置场景（`classic` / `nested-chain` / `deadlock`） |
| GET | `/api/demos/{id}` | 查看某个内置场景的定义 |
| GET | `/api/demos/{id}/simulate?inherit=true\|false` | 单次运行内置场景（默认 true） |
| GET | `/api/demos/{id}/compare` | 同一内置场景开/关 PIP 各跑一次并对比 |
| POST | `/api/simulate` | 单次模拟，body 为 `Config` |
| POST | `/api/compare` | 同一负载开/关 PIP 各跑一次，body 为 `{tasks, maxTime?}` |

### 请求格式

`Config`（`POST /api/simulate`）：

```json
{
  "enableInheritance": true,
  "maxTime": 100000,
  "tasks": [
    {
      "name": "H",
      "priority": 3,
      "releaseTime": 1,
      "steps": [
        {"op": "lock", "resource": "A"},
        {"op": "compute", "duration": 3},
        {"op": "unlock", "resource": "A"}
      ]
    }
  ]
}
```

- `priority`：整数，**数值越大优先级越高**。
- `releaseTime`：任务最早可运行的 tick（整数时间，`t` 表示半开区间 `[t, t+1)`）。
- `steps`：固定程序，只接受三种操作：
  - `{"op":"compute","duration":N}` —— 占用 CPU N 个 tick；
  - `{"op":"lock","resource":"A"}` —— 获取互斥锁（被占则阻塞）；
  - `{"op":"unlock","resource":"A"}` —— 释放锁。
- `lock/unlock` 是**瞬时操作**，在 tick 边界立即处理，不消耗时间；只有 `compute` 消耗 tick。
- 嵌套加锁允许（如先 A 后 B），但程序结束时必须释放全部持有的锁，且不能 unlock 未持有的锁；
  校验失败返回 HTTP 400。
- `POST /api/compare` 的 body 只含 `tasks` 和可选 `maxTime`（误传 `enableInheritance`
  会被严格 JSON 解析以 400 拒绝，便于发现拼写错误）。

### 响应要点

- `events[]`：**每一步调度决策**的完整轨迹，事件类型包括
  `RELEASE / SCHEDULE / IDLE / COMPUTE / LOCK / BLOCK / WAKE / UNLOCK / FINISH /
  PRIORITY_INHERIT / PRIORITY_RESTORE / DEADLOCK / TIMEOUT`。
  每个 `SCHEDULE` 都列出当拍可运行任务及其有效优先级、被抢占者、该拍是否消耗 tick。
- `tasks[]`：每任务的完成时间、`runningTicks`、`blockedTicks`（阻塞总 tick 数）、
  每次锁请求的 `requestedAt / acquiredAt / waitedTicks`。
- `/api/compare` 额外给出 `deltas[]`（逐任务阻塞差）和
  `highestPriorityWaitingTask` 等汇总字段。

### curl 请求样例

```bash
# 1) 经典场景：开启 PIP 单次运行
curl -s -X POST http://127.0.0.1:8080/api/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/classic-simulate.json

# 2) 经典场景：开/关 PIP 对比（阻塞时长）
curl -s -X POST http://127.0.0.1:8080/api/compare \
  -H 'Content-Type: application/json' \
  -d @examples/classic-compare.json

# 3) 嵌套锁链（传递继承）对比
curl -s -X POST http://127.0.0.1:8080/api/compare \
  -H 'Content-Type: application/json' \
  -d @examples/nested-chain-compare.json

# 4) 内置场景快捷方式，无需请求体
curl -s http://127.0.0.1:8080/api/demos/classic/compare
curl -s 'http://127.0.0.1:8080/api/demos/nested-chain/simulate?inherit=true'
curl -s 'http://127.0.0.1:8080/api/demos/deadlock/simulate?inherit=true'
```

样例请求文件位于 `examples/`，真实运行的完整 JSON 响应已保存在 `examples/output/`。

---

## 3. 验收场景与实际运行结果

以下结果为**实际运行服务得到**（非手写），完整轨迹见 `examples/output/*.trace.txt`
与对应 `.json`。

### 场景 A：经典优先级反转 + 中优先级干扰（`classic`）

- L（优先级 1）在 t=0 拿到锁 A，临界区 4 个 tick；
- H（优先级 3）在 t=1 释放，立即请求 A，被 L 阻塞；
- M（优先级 2）在 t=2 释放，纯 CPU 密集 5 个 tick。

| 指标 | 关闭 PIP | 开启 PIP |
|---|---|---|
| H 阻塞时长 | **8 tick** | **3 tick** |
| H 完成时间 | t=12 | **t=7** |
| 干扰说明 | t=2 起 M 连续抢占 L 5 tick，H 被间接阻塞 | L 在 t=1 继承优先级 3，M 从未抢到 CPU，t=4 释放 A 后 L 恢复为 1 |

轨迹关键片段（PIP 开启，摘自实际输出）：

```
t=1  BLOCK             H  waits on A held by L
t=1  PRIORITY_INHERIT  L  prio 1->3 due to H
t=2  SCHEDULE          L  runnable=[L:3,M:2]      <- M 已释放，但抢不过被提升的 L
t=4  UNLOCK            L  A
t=4  PRIORITY_RESTORE  L  prio 3->1
t=4  WAKE              H  A                        <- 直接交接，同一边界恢复执行
t=7  FINISH            H
```

关闭 PIP 时：t=2 M 抢占 L 一直运行到 t=7，L 直到 t=9 才释放 A，H 全程阻塞
（`blockedTicks=8`）。

### 场景 B：嵌套锁链的传递继承（`nested-chain`）

- L（1）持有 B；X（2）持有 A 后再请求 B，因而阻塞；H（3）请求 A，阻塞在 X 上；
- 干扰者 M（2）在 t=4 释放。

捐赠必须沿 H → X → L **传递**，实际结果：

| 任务 | 关闭 PIP 阻塞 | 开启 PIP 阻塞 |
|---|---|---|
| H（最高等待者） | **12 tick** | **7 tick** |
| X | **10 tick** | **5 tick** |

PIP 轨迹中可以看到两级提升事件 `X prio 2->3 due to H` 与 `L prio 1->3 due to X`
（传递），锁链按 B、A 的逆序解开时逐级发出 `PRIORITY_RESTORE`。

### 场景 C：死锁检测（`deadlock`，非 PIP 可解）

L 持 A 求 B、H 持 B 求 A，形成等待环。开/关 PIP 两次运行都在 t=4 输出：

```json
{"time":4,"kind":"DEADLOCK","cycle":["L","H"],
 "message":"cyclic mutex wait detected; priority inheritance does not prevent deadlock"}
```

响应状态为 `"status":"deadlocked"`，模拟器立即终止而不是空转——这如实反映了
PIP 的边界：**它限制优先级反转，但不防止死锁**（那是 PCP/优先级天花板协议的范畴）。

---

## 4. 运行测试

```bash
go test ./...            # 单元 + HTTP 接口测试
go test -v ./...         # 详细输出
go test -race ./...      # 竞态检测
go vet ./...
gofmt -l .               # 应无输出
```

测试覆盖（`simulator/engine_test.go`、`main_test.go`）：

- 经典场景 H 阻塞 3 vs 8 的精确断言；
- 嵌套链 H 阻塞 7 vs 12、X 5 vs 10，且 L、X 都出现提升到 3 的事件；
- 锁释放后优先级恢复事件，且恢复后 H 先于 L 的尾部工作完成；
- 死锁环检测（开/关 PIP 均报 `cycle=[L,H]`）；
- 同一输入连续 6 次运行的 JSON 轨迹完全一致（确定性）；
- 释放前空转（IDLE 跳变）、单任务、嵌套加锁、11 类输入校验错误、maxTime 超时；
- HTTP 层：健康检查、405/404/400、严格 JSON 未知字段拒绝、全部 demo 路由。

实测记录（本机 `go1.23.4 linux/amd64`，`go test ./...`）：

```
ok  	pip-sim	0.008s
ok  	pip-sim/simulator	(cached)
```

---

## 5. 实现说明与建模约定

- **离散时间驱动循环**：每个时间边界依次执行 释放到期任务 → 重算有效优先级（固定点）
  → 选出最高有效优先级任务（索引小者平局占先，保证确定性）→ 执行一个原语；
  瞬时原语（lock/unlock/交接）在同一边界反复决策，不推进时间。
- **有效优先级**：每个边界先重置为基优先级，再沿「阻塞者 → 持有者」边迭代捐赠到
  固定点，因此天然支持任意深度的锁链；与上一边界不同即发提升/恢复事件。
- **阻塞记账**：每消耗一个 compute tick，所有 BLOCKED 任务 `blockedTicks+1`；
  锁请求级等待时间记录在 `lockAttempts[]`。
- **唤醒策略**：unlock 时从该锁的等待者中选有效优先级最高者直接交接，
  在**同一时间边界**重新参与调度（避免不必要的 tick 误差）。
- **无操作时**：若所有未完成任务都阻塞且存在等待环 → DEADLOCK；
  否则跳到下一个释放时间并记 IDLE。`maxTime`（默认 100000）兜底。

---

## 6. 已完成 / 未完成项

**已完成**

- [x] 确定性离散时间调度内核（单 CPU、固定优先级、可抢占）
- [x] 直接优先级继承 + 嵌套锁链的传递继承（固定点传播）
- [x] 锁释放的直接交接与提升后优先级的正确恢复（含事件轨迹）
- [x] 高/中/低三任务经典反转场景：实测阻塞 8 → 3 tick
- [x] 嵌套锁链场景：实测 H 阻塞 12 → 7 tick
- [x] 每一步运行决策的结构化输出（JSON 事件流）
- [x] 开/关 PIP 一键对比接口与逐任务阻塞差
- [x] 死锁环检测、IDLE 跳变、超时保护、严格输入校验
- [x] `net/http` JSON 服务、curl 样例、内置场景快捷路由
- [x] 自动化测试（内核 + HTTP），实际跑通
- [x] 仅标准库，`go.mod` 锁定；无 `go.sum` 第三方条目

**未做（范围之外或可后续扩展）**

- [ ] 未实现优先级天花板协议 PCP（本任务只要求 PIP）；因此不预防死锁，只检测。
- [ ] 单 CPU 模型，未建模多处理器 / 缓存相关开销。
- [ ] 任务程序为提交时的固定序列，不支持运行期动态创建任务或周期任务（periodic jobs）。
- [ ] 时间为整数 tick，未实现任意截止期（deadline）、响应时间分析（RTA）等指标导出。
- [ ] 纯后端，无前端界面（按需求刻意不做）。
- [ ] 未做鉴权/限流：服务定位为本地模拟工具，请勿直接暴露到公网。

## 7. 目录结构

```
.
├── go.mod                          # 模块定义（依赖锁定：仅标准库）
├── main.go                         # net/http 服务入口与路由
├── main_test.go                    # HTTP 接口测试
├── README.md
├── examples/
│   ├── classic-simulate.json       # 经典场景单次请求样例
│   ├── classic-compare.json        # 经典场景对比请求样例
│   ├── nested-chain-compare.json   # 嵌套锁链对比请求样例
│   └── output/                     # 实际运行保存的响应与可读轨迹
└── simulator/
    ├── types.go                    # 请求/配置类型
    ├── engine.go                   # 调度内核（继承、抢占、交接、死锁检测）
    ├── event.go                    # 事件的文本渲染（Trace）
    ├── result.go                   # 结果汇总与开/关对比
    ├── scenarios.go                # 三个内置验收场景
    ├── validate.go                 # 静态输入校验
    └── engine_test.go              # 内核自动化测试
```
