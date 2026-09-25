# 验证记录（VALIDATION）

本文件如实记录开发完成时的实际运行命令与结果。环境：

- 时间：2026-09-23（UTC）
- OS：Linux 6.8.0-90-generic (amd64)
- 工具链：`go version go1.22.2 linux/amd64`
- 第三方依赖：**无**（仅 Go 标准库）

原始输出保存在 `docs/output/`：

- `validation-summary.txt`：go/vet/test 汇总
- `test-report.txt`：`go test -v` 用例级报告
- `race.txt`：`go test -race` 报告
- `http-e2e.txt`：真实 HTTP 服务的 curl 端到端记录
- `<scenario>.json` / `req-*.json`：各场景与请求样例的完整 Result JSON
- `<scenario>-timeline.md`：可读时间线表（汇总见 `TIMELINES.md`）

---

## 1. 静态检查与自动化测试

命令：

```bash
go vet ./...
go test ./... -count=1
go test -race ./... -count=1
```

结果：**全部通过，无告警**。

- `go vet ./...` → clean
- `go test ./...` → 四个含测试的包全部 `ok`
- `go test -race ./...` → 全部 `ok`，无数据竞争

共 **22 个测试函数，全部 PASS**，分布：

| 包 | 测试函数数 | 断言内容 |
|---|---:|---|
| clock | 3 | 虚拟时钟推进、负向推进 panic、确定性 WallTime |
| executor | 1 | 注入的录制执行器捕获 CPU 运行序列与继承优先级 |
| httpapi | 6 | health、场景列表、内置场景、404、自定义提交、400 非法请求 |
| scheduler | 12 | 见下 |

scheduler 包 12 个用例：

1. `TestClassicInversionWithoutPI` — 无继承时 `inversionTicks>0`，High 阻塞 ≥8 tick；
2. `TestClassicInversionFixedWithPI` — 继承时反转为 0，High 完成时刻严格提前，Low 曾达到 eff=3；
3. `TestThreeLevelTransitiveInheritance` — Low 转换 `1→3→4→1`、High `3→4→3`；
4. `TestEffectivePriorityRecomputedOnRelease` — 释放即回落（reason=`grant`）；
5. `TestMultiLockReleaseOrders` — 两种释放顺序下 `lock_grant` 顺序相反、等待者阻塞 tick 数相反；
6. `TestDeadlockDetected` — AB/BA 环在正确时刻检出，事件环闭合，两任务停在 blocked；
7. `TestDeadlockDetectionDisabledTerminatesWithError` — 关闭检测时不死循环，以 `error` 终止且无 deadlock 事件；
8. `TestPreemptionByArrival` — 新到达高优先级任务在准确 tick 抢占；
9. `TestValidationErrors` — 空任务、重复 ID、未声明资源、非法动作均被拒绝；
10. `TestProgramErrorReleaseNotOwned` — 释放非自有锁产生 `program_error`；
11. `TestDeterminism` — 两次运行事件逐字段（`reflect.DeepEqual`）相等；
12. `TestEventSequenceMonotonic` — seq 稠密从零开始、时间不回退。

> 计数以 `docs/output/test-report.txt` 的实际 `--- PASS` 行为准。

---

## 2. 七个场景的 CLI 实际运行

命令形态：`go run ./cmd/pim -scenario <name>`（等价于编译后的二进制）。
下表是真实输出汇总：

| 场景 | makespan | inversionTicks | 阻塞 ticks | 完成时刻 | 死锁环 |
|---|---:|---:|---|---|---|
| classic-inversion-**no-pi** | 14 | **4** | High=9 | Medium=8, High=**13**, Low=14 | — |
| classic-inversion-**pi** | 14 | **0** | High=5 | High=**9**, Medium=13, Low=14 | — |
| three-level-inheritance | 12 | 0 | High=7, Urgent=8 | Low=8, High=10, Urgent=12 | — |
| multi-lock-**r1-first** | 10 | 0 | A=4, B=4 | B=8, A=9, Low=10 | — |
| multi-lock-**r2-first** | 10 | 0 | A=6, **B=3** | **B=7**, A=9, Low=10 | — |
| deadlock-ab-ba | 1（停机） | 0 | — | — | **T1→T2→T1** @t=1 |
| inherit-restore | 7 | 0 | High=2 | High=5, Low=7 | — |

关键对照（验收点）：

- **经典反转**：无 PI 时 Medium 在 t=5..8 占用 CPU，High 被拖到 t=13；
  开 PI 后 Low 在临界区以 eff=3 运行，Medium 无法插队，High t=9 完成，
  `inversionTicks` 由 4 降为 0。
- **三级传递继承**：真实 `priority_change` 序列为
  `Low 1→3 @t1`（High 阻塞 R1）、`Low 3→4 @t2` 与 `High 3→4 @t2`
  （Urgent 阻塞 R2）、`Low 4→1 @t8`、`High 4→3 @t10`。
- **多锁释放顺序**：先放 R2 时 B 仅阻塞 3 tick、t=7 完成；先放 R1 时
  B 阻塞 4 tick、t=8 完成，且 A 的阻塞数对应相反。
- **死锁反例**：PI 无法避免 AB/BA；t=1 形成环并停机，事件含
  `edges=[{waiter:T1,holds:R2,owner:T2},{waiter:T2,holds:R1,owner:T1}]`。

CLI 退出码（编译后二进制实测）：

| 情形 | 退出码 |
|---|---:|
| 正常完成 | 0 |
| `deadlock-ab-ba -fail-deadlock` | 2 |
| 非法 Spec（引用未声明资源） | 1 |
| 未知场景名 | 1 |

---

## 3. 请求样例文件运行

`examples/` 下 4 个 JSON 均通过 CLI 与 HTTP 两种方式实际运行：

```bash
for f in examples/*.json; do
  go run ./cmd/pim -file "$f" > "docs/output/req-$(basename "$f" .json).json"
done
```

对应输出保存在 `docs/output/req-*.json`，与同参数内置场景结果一致（确定性）。

---

## 4. HTTP 端到端实测

启动：`pim-server -addr 127.0.0.1:<port>`（端口由系统分配以避免环境占用）。
原始记录见 `docs/output/http-e2e.txt`，摘要：

| 请求 | 结果 |
|---|---|
| `GET /healthz` | `200 {"status":"ok"}` |
| `GET /api/scenarios` | `200`，返回 7 个场景 |
| `POST /api/simulate`（classic no-PI 样例） | `200`，`makespan=14, inversionTicks=4, completed={Medium:8, High:13, Low:14}` |
| `GET /api/scenarios/deadlock-ab-ba` | `200`，`deadlock.cycle=[T1,T2,T1], at=1` |
| `GET /api/scenarios/nope` | `404` |
| `POST /api/simulate` 含未知字段 | `400 invalid JSON spec: json: unknown field "bogus"` |

> 开发期间发现本机 18080/18091 端口被无关进程（`vccsim`）占用；这不是本
> 服务的问题，改用系统临时端口后所有接口验证通过。

---

## 5. 已知边界与如实声明

- 模型为**离散事件、单处理器、协作式临界区即时操作**：`acquire/release`
  不消耗 tick，`cpu` 每次只推进 1 tick 以实现即时抢占；这是教学模型而非
  周期实时任务可调度性分析（无 EDF/RM、无优先级天花板协议 PCP）。
- 锁为**非递归**：同任务重取自己持有的锁按 `program_error` 处理。
- 关闭死锁检测时，封闭模型中的等待环不存在合法出路；系统选择以
  `Result.error` **终止而非挂起**（已在测试中固定该行为）。
- `inversionTicks` 的口径为“无关中间优先级任务正在 CPU 上、而更高优先级
  就绪者阻塞在他人临界区”的 tick 数；它是反转强度指标，不等于总阻塞时长。
- 本轮验证**无未通过项**；`go vet`、`go test`、`go test -race` 全部干净。
