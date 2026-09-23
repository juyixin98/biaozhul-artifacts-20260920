# 工作负载排空规划（Workload Drain Planner）

纯后端服务：基于**本地对象快照**规划 Kubernetes 节点排空（drain），计算
Deployment 副本、Pod 就绪状态与 PDB 允许中断数，按明确顺序分批迁出，
并在**本地模拟器**上真实执行（不接触任何真实集群）。

- Python 3.12 + FastAPI + Pydantic v2
- 密码操作真实执行：**Ed25519** 签名/验签（`cryptography` 库）、SHA-256 摘要、一次性 nonce 防重放
- 无任何桩实现：PDB admission、控制器重建、调度、就绪流转全部真实模拟

---

## 1. 它解决什么问题

对一个节点（或多个节点同时）做 drain 时，必须回答：

1. 节点上哪些 Pod **可以迁**、哪些**不能迁**（DaemonSet、mirror、裸 Pod、emptyDir…）；
2. 每个 PDB **现在允许中断几个 Pod**；一个 Pod 被多个 PDB 同时选中时，**所有**覆盖它的 PDB 都必须许可（交集）；
3. 迁出必须**分批（波次）**：上一批旧 Pod 消失、替代副本 Ready 之后，下一批才能开始；
4. **删除旧 Pod 绝不等于补齐副本**——必须等到一个 uid 不同、运行在非排空节点上的 Ready 替代 Pod；
5. 副本已经不健康、替代 Pod 迟迟不就绪、无处可调度时，**停下来报告，而不是硬闯**；
6. 执行期间对象快照发生代次变化，基于最新快照**重算**；随时可以**取消**。

每一个计划步骤都带有明确的**前置条件（preconditions）**与**完成判据（completion）**，
执行器在动作发起前对实时快照重新校验前置条件。

## 2. 目录结构

```
app/
  models.py      快照输入模型（Node/Pod/Deployment/PDB），Pydantic 校验
  crypto.py      规范化 JSON、SHA-256、Ed25519 签名/验签
  planner.py     PDB 计算、Pod 分类、波次分批、计划生成（纯函数）
  simulator.py   本地集群模拟器（cordon/evict/控制器重建/调度/就绪/故障注入/代次）
  executor.py    执行器：波次门禁、实时前置校验、重算、超时停滞、取消、完成度校验
  store.py       全局状态、nonce、客户端公钥、签名信封校验
  main.py        FastAPI 路由
  client.py      签名客户端（测试/验收脚本复用）
examples/        4 份示例快照
scripts/
  acceptance.py  对真实 HTTP 服务的端到端验收（四大场景 + 协议，34 项检查）
  acceptance.sh  一键：装依赖 → 起服务 → pytest → 端到端验收 → 停服务
tests/           46 个自动化测试
requirements.txt 锁定依赖（全量 freeze）
```

## 3. PDB 语义（与 Kubernetes 对齐）

对每个 PDB：

- `selected`：namespace + label selector 命中、phase 非 Succeeded/Failed 的 Pod；
- `expectedCount`：其中没有 deletionTimestamp 的 Pod；
- `currentHealthy`：expected 中 `ready=true` 的 Pod；
- `disruptionsInProgress`：已拿到 deletionTimestamp 的被选中 Pod；
- `desiredHealthy`：
  - `minAvailable: N` → `N`；`"p%"` → `ceil(expected*p/100)`；
  - `maxUnavailable: N` → `expected-N`；`"p%"` → `expected-floor(expected*p/100)`；
- `disruptionsAllowed = max(0, healthy-desired) - terminating`；
  healthy < desired（副本本就不健康）时直接为 0。该值在在途中断窗口可能为负。

**多 PDB 交集**：驱逐一个 Pod 前，对每一个覆盖它的 PDB 计算当前
disruptionsAllowed，必须全部 ≥ 1 才受理；否则模拟 apiserver 返回 429 式拒绝
（本系统中表现为 `PdbAdmissionError`，执行器软等待，不硬删）。

波次划分：同一波内每个 PDB 占用的驱逐数 ≤ 其初始预算；无 PDB 的 Pod 按控制器
每波每控制器至多 1 个。跨节点同时 drain 时预算是**全局共享**的（两个节点上属于
同一 PDB 的 Pod 不会在同一波被一起驱逐）。

## 4. 计划步骤与前置条件

步骤顺序固定：

1. `cordon`（wave -2）：每个排空节点一个；
2. `delete_terminal`（wave -1）：Succeeded/Failed 的终态 Pod；
3. `evict`（wave 0..N）：按波次排序的驱逐。

每个 evict 步骤带：

- 前置条件：`node_cordoned`、`pod_present`、`pdb_intersection_admit`、
  Deployment  Pod 额外有 `replacement_schedulable`；
- `wave_gate`：上一波全部驱逐已完成（旧 Pod 消失 **且** 替代 Ready）；
- `completion`：原 Pod 消失，且同一 Deployment 在非排空节点存在 **uid 不同**、
  Running+Ready 的替代 Pod。每个替代 Pod 只能被一个步骤“认领”，杜绝重复计数。

不可迁出的 Pod：`mirror_pod`、`daemonset_pod`、`bare_pod`（无控制器）、
`owner_not_controller`、`local_storage_empty_dir`、`deployment_not_found`、
`pod_terminating`。默认进入 `blocked`；`force=true` 时进 `skipped` 并继续其余部分。

## 5. 模拟器与代次（generation）

模拟器真实执行：cordon/uncordon、eviction admission、grace 期满删除、
Deployment 每 tick 对账补副本、Pending→Running（按节点 schedulable/capacity 调度）
→Ready（延迟可配）。

- `generation`（快照代次）：**外部/结构性**动作 +1（载入快照、cordon/uncordon、
  驱逐受理、终态删除、外部对象漂移）。控制器补副本、grace 期满删除、就绪流转
  属于正常控制器行为，**不** bump。
- `fault_generation`：故障注入/清除单独计数，是执行器预期内的控制操作，不触发重算。
- 执行器发现 generation 与上次结算不一致 → 用**宽松模式**重建剩余步骤
  （瞬时零预算/Pod terminating 不再硬阻塞，由实时 admission 闸门把关），
  保留已完成/在途状态；若出现新的结构性不可迁出项则停滞。

故障注入：`never_ready`（替代 Pod 永不就绪）、`delay_ready`（拉长就绪延迟）、
`clear`（恢复）。

## 6. 密码协议（真实执行）

所有写接口使用签名信封：

```json
{ "kid": "...", "nonce": "...", "ts": 1727..., "payload": { ... }, "sig": "<hex>" }
```

- 密钥：Ed25519（`cryptography` 库，非自造密码学）；`kid = sha256(SPKI DER)[:16]`；
- 签名内容：规范化 JSON（键排序、无空白）的 `{kid,nonce,ts,payload}`；
- nonce 一次性（服务端消费即删，重放 → 401），时间戳 ±300s 窗口；
- 所有响应由服务端 Ed25519 私钥签名，客户端用 `/api/server-key` 的公钥验签；
- 计划对象本身也带 `server_signature`，篡改 drain_nodes 等字段后验签失败。

引导流程：`GET /api/server-key` → `POST /api/client-keys`（注册客户端公钥）→
`POST /api/nonces`（每个写请求取一个新 nonce）。`app/client.py` 已封装好。

## 7. HTTP API

| 方法 | 路径 | 说明 | 签名 |
|---|---|---|---|
| GET | `/healthz` | 健康检查 | 否 |
| GET | `/api/server-key` | 服务端 Ed25519 公钥/kid | 否 |
| POST | `/api/client-keys` | 注册客户端公钥 | 否（引导） |
| POST | `/api/nonces` | 领取一次性 nonce | 否（引导） |
| POST | `/api/snapshot` | 载入快照 | 是 |
| GET  | `/api/snapshot` | 当前模拟器快照 + generation + 摘要 | 签名响应 |
| POST | `/api/plans` | 生成计划（可带 snapshot，同时载入模拟器） | 是 |
| GET  | `/api/plans/{id}` | 取计划（含服务器签名） | 签名响应 |
| POST | `/api/executions` | 从计划启动执行 | 是 |
| GET  | `/api/executions/{id}` | 执行状态（每步骤实时状态/前置条件/live PDB） | 签名响应 |
| POST | `/api/executions/{id}/advance` | 推进一个 tick | 是 |
| POST | `/api/executions/{id}/cancel` | 取消（可选 uncordon） | 是 |
| POST | `/api/executions/{id}/resume` | 停滞条件解除后恢复 | 是 |
| POST | `/api/sim/tick` | 仅推进模拟器时间 | 是 |
| POST | `/api/sim/fault` | 注入/清除就绪故障 | 是 |
| GET  | `/api/events` | 模拟器事件流 | 签名响应 |

服务自带交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`（但写接口需要
签名信封，推荐直接用 `app/client.py` 或验收脚本调用）。

## 8. 本地启动

需要 Python 3.10+（开发与验证在 3.12）。

```bash
cd P086/a
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 已锁定版本

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另一终端：
curl -s http://127.0.0.1:8000/healthz
```

## 9. 验收命令

一键验收（自动建 venv、装锁定依赖、起服务、跑 pytest、跑端到端、停服务）：

```bash
bash scripts/acceptance.sh
```

或分步：

```bash
source .venv/bin/activate
python -m pytest -q                       # 46 个单元/集成测试

uvicorn app.main:app --port 8000 &        # 启动服务
python scripts/acceptance.py              # 34 项端到端检查（默认 http://127.0.0.1:8000）
# 自定义地址：DRAIN_URL=http://127.0.0.1:8077 python scripts/acceptance.py
```

### 验收覆盖（`scripts/acceptance.py` 实际断言）

- **协议**：Ed25519 计划签名可验签、篡改即失败；nonce 重放 401、payload 篡改 401、
  过期时间戳 401、未知 kid 401；
- **场景 1 已不健康副本**：1 Ready / 2 不就绪、minAvailable=2 → 预算 0，
  计划 blocked、禁止启动执行；
- **场景 2 两节点同时排空**：6 副本跨两节点、minAvailable=5 → 6 个波次每波 1 个；
  逐 tick 断言在途中断 ≤ 1、Ready 副本全程 ≥ 5；结束时两节点清空、6 个 Ready
  替代副本全部落在第三节点、两节点均 cordon；
- **场景 3 替代 Pod 迟迟未就绪**：注入 never_ready → 第一步超时停滞
  （`step_timeout:replacement_not_ready`），后续波次绝不启动；旧 Pod 已删除但
  替代 NotReady 时步骤不算完成；清除故障 + resume 后跑完整 drain；
- **场景 4 取消**：取消后未开始步骤全部 cancelled、不再发起任何新动作，
  `uncordon_on_cancel=true` 时节点恢复可调度。

另含示例 `examples/no_capacity.json`：替代副本无处调度时执行停滞于
`step_timeout:no_schedulable_node`；`force=true` 时跳过裸 Pod/DaemonSet 后完成。

## 10. 示例输入

| 文件 | 演示点 |
|---|---|
| `examples/two_node_drain.json` | 两节点同时排空、跨命名空间 PDB、终态 Job、DaemonSet/裸 Pod 阻塞（force 可跳过） |
| `examples/multi_pdb.json` | 一个 Pod 同时被两个 PDB 选中，取更紧的约束分批 |
| `examples/unhealthy.json` | 仅 1/3 Ready，minAvailable=2 → 预算 0、禁止迁出 |
| `examples/no_capacity.json` | 唯一其他节点不可调度且容量满 → 执行期停滞 |

快速试算（不起服务，纯函数）：

```bash
python - <<'PY'
import json
from app.planner import build_plan
snap = json.load(open("examples/multi_pdb.json"))
plan = build_plan(snap, ["node-a"])
print(plan["status"], plan["waves"])
for s in plan["steps"]:
    print(s["index"], s["kind"], s["wave"], s.get("pod") or s.get("node"))
PY
```

## 11. 设计取舍与边界

- 这是**本地快照规划器 + 模拟器**，不调用真实 Kubernetes API；执行器所有动作只落在
  `Simulator` 内存对象上，重启进程即清空。
- label selector 仅实现等值匹配（`matchLabels` 语义），未实现 set-based 表达式。
- 调度模型是容量槽位（capacity）+ schedulable 的简化模型，不模拟亲和性/污点/资源量。
- 单进程内存状态、单模拟器实例；无持久化（目标是规划正确性验证，而非多租户服务）。
