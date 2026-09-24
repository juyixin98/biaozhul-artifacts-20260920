# 工作负载排空规划器 (Workload Drain Planner)

基于**本地对象快照**的 Kubernetes 节点排空计划器与执行器，纯后端（Python + FastAPI），
不连接任何真实集群——执行器只操作进程内的**本地模拟器**。

它计算 Deployment 副本数、Pod 就绪状态与 PodDisruptionBudget（PDB）允许中断数，
按**明确顺序分批迁出**，尊重多个 PDB 的交集和不可迁出 Pod，
**绝不把"删除中的 Pod"当作已补齐的副本**，并为每一步输出前置条件。

---

## 1. 它保证什么

1. **Deployment 副本真实补齐**：驱逐一个控制器 Pod 后，模拟器立刻在可调度节点上
   创建一个 `Pending` 的替代 Pod；该替代 Pod 经过 `readyDelay` 个 tick 才变 `Ready`。
   删除中的旧 Pod 单独计为 `disrupted`，在它消失前**不**算作健康副本。
2. **PDB 多集合交集**：一个 Pod 可被多个 PDB 选中。每一波对每个候选 Pod 检查
   *所有*选中它的 PDB；同一波内先前准入的 Pod 立即占用预算，因此一个波次不可能
   突破任何一个 PDB。支持 `minAvailable`（整数或 `"n%"`，百分比向上取整）、
   `maxUnavailable`，以及 `unhealthyPodEvictionPolicy`（`IfHealthyBudget` /
   `AlwaysAllow`）。
3. **明确顺序的分批**：`Cordon(每个目标节点)` → 若干 `EvictWave` → `Complete`。
   波次之间有硬屏障：上一波的 Pod 必须已被垃圾回收，且上一波的替代 Pod 必须已
   `Ready`，下一波才允许执行。
4. **不可迁出 Pod**：`mirrorPod` / `staticPod` 永远上报为 `BLOCKED` 阻断项；
   DaemonSet Pod 默认跳过（`ignoreDaemonSets`），不阻断完成；排空全部节点导致
   替代 Pod 无处可调度时也判为阻断。
5. **每一步前置条件**：每个步骤返回带 `id / 描述 / 是否满足 / 细节 / 严重级`
   的前置条件列表；任何 `hard` 条件不满足，该步骤**绝不执行**（只推进时钟等待）。
6. **快照代次变化即重算**：模拟器每次变更都单调递增 `generation`。执行器在每步前
   用最新快照重建当前波次；带外（out-of-band）变更会抬高 `externalGeneration`
   水位并使旧计划令牌失效，客户端必须取新令牌后继续。
7. **真实密码学**：计划令牌是真实的 `HMAC-SHA256`（`hmac.compare_digest` 校验），
   载荷绑定 `drain / 带外水位 / 步骤游标 / tick`，可检测伪造、重放与跨 drain 误用。
8. **执行后审计**：模拟器在每次转换后运行 PDB 安全审计，一旦健康数会跌破承诺底线
   直接抛出 `AuditError`，而不是掩盖问题。

---

## 2. 目录结构

```
app/
  models.py      # 快照/对象/PDB/计划/前置条件等领域模型
  eviction.py    # PDB 记账、多 PDB 交集准入 admit_evictions、安全审计
  planner.py     # 影子模拟预测完整有序计划；运行时 next_wave
  simulator.py   # 唯一被执行器操作的本地"集群"（含外部变更接口与审计）
  executor.py    # 状态机：前置条件、波次屏障、取消、代次重算、令牌
  security.py    # HMAC-SHA256 令牌、SHA-256 指纹（真实密码学原语）
  manager.py     # 进程内 simulation/drain 注册表（线程安全、幂等键）
  api.py         # FastAPI 路由（JSON）
  main.py        # 应用入口与异常处理
examples/        # 三份示例快照
scripts/
  demo.py                # 库级四场景验收（无需起服务）
  curl-walkthrough.sh    # 真实 HTTP 走查
tests/           # 60 个自动化测试（单元 + 执行器验收 + HTTP + 密码学）
requirements.txt / requirements.lock / pyproject.toml
```

---

## 3. 本地启动

需要 Python 3.10+（开发验证于 3.12）。

```bash
# 1) （可选）建虚拟环境
python3 -m venv .venv && . .venv/bin/activate

# 2) 安装锁定依赖
python3 -m pip install -r requirements.lock
#    运行测试则安装开发依赖（含 pytest）：
python3 -m pip install -r requirements-dev.lock

# 3) 启动服务
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开交互式 API 文档：<http://127.0.0.1:8000/docs>

---

## 4. 验收命令

```bash
# A) 全部自动化测试（60 个）
python3 -m pytest -q

# B) 四个指定场景的库级验收（逐步打印前置条件；任何违规则非零退出）
python3 scripts/demo.py

# C) 真实 HTTP 走查：先启动服务，再执行
BASE="http://127.0.0.1:8000/api/v1" bash scripts/curl-walkthrough.sh
```

`demo.py` 覆盖：

| 场景 | 期望 |
| --- | --- |
| 1. 单节点排空（3 副本、minAvailable=2） | 逐波完成，node-a 清空，审计为空 |
| 2. 两节点同时排空（3 个 PDB，含跨两套服务的共享 PDB） | 每波恰 1 个 Pod，最终 8 个就绪，全程不越界 |
| 3. 替代 Pod 迟迟不就绪（readyDelay≈∞） | 排空停在 `WAITING`，不假完成，不突破 PDB |
| 4. 排空中途取消 | 拒绝后续驱逐、节点保持 cordon、在途替代自愈且审计为空 |

---

## 5. HTTP 协议摘要

所有接口前缀 `/api/v1`。计划令牌通过响应头 `X-Plan-Token` 返回，
执行类接口需在请求头 `X-Plan-Token` 中携带。

| 方法 & 路径 | 作用 |
| --- | --- |
| `POST /simulations` | 用 `{"snapshot": {...}}` 创建一个本地模拟集群 |
| `GET  /simulations/{id}/snapshot` | 读取当前快照（带 `ETag`） |
| `POST /simulations/{id}/tick` | 推进逻辑时钟 `{"steps": n}`（驱动就绪/GC） |
| `POST /simulations/{id}/pods/ready` | **带外**设置某 Pod 就绪与否（抬升水位） |
| `POST /simulations/{id}/pods` | **带外**新增 Pod |
| `GET  /simulations/{id}/audit` | 当前 PDB 违规列表（空即安全） |
| `POST /simulations/{id}/drains` | 针对若干节点生成排空计划（支持 `Idempotency-Key`） |
| `GET  /simulations/{id}/drains/{d}` | 查看状态、当前步骤、前置条件，并取最新令牌 |
| `POST /simulations/{id}/drains/{d}/advance` | 单步执行；`{"tickWait": n}` 先等待最多 n tick |
| `POST /simulations/{id}/drains/{d}/autorun` | 在总 tick 预算内自动推进到完成/阻断/等待 |
| `POST /simulations/{id}/drains/{d}/cancel` | 取消：停止驱逐、节点保持 cordon |

错误体统一为 `{"error": {"code": ..., "message": ...}}`，常见码：
`invalid_token`(401)、`stale_step`/`stale_generation`(409，响应头带新的
`X-Plan-Token`)、`blocked`、`cancelled`、`unknown_sim`(404)。

### 最小请求示例

```bash
curl -s -X POST localhost:8000/api/v1/simulations \
  -H 'content-type: application/json' \
  -d "{\"snapshot\": $(cat examples/snapshot-basic.json)}"
```

---

## 6. 关键语义说明（与真实 Kubernetes 对齐/取舍）

- **PDB 记账**（对齐 `disruption.go` 的保守建模）：
  - `expected` 取控制器 `spec.replicas`（裸 Pod 各计 1）；删除中的 Pod 在物理
    消失前仍占用副本槽，其替代 Pod 也承诺同一槽，故 `expected` 不因在途中断缩水。
  - 准入判定使用**固定承诺底线**：
    - `minAvailable=R`：健康数（扣除本波已假设删除者）必须 `≥ R`；
    - `maxUnavailable=U`：健康数必须 `≥ expected − U`。
    把本波候选折成"假设终止"后与该底线比较，即同时正确处理在途中断与同波累积。
  - 百分比按 K8s 习惯**向上取整**。
- **不健康副本**：`IfHealthyBudget`（默认）下，若工作负载健康数已等于底线，
  不健康 Pod 不允许驱逐（状态为 `WAITING`，等它恢复或外部处置）；
  `AlwaysAllow` 下不健康 Pod 总是可驱逐。
- **取消不是回滚**：取消后节点保持 `cordon`，已发出的在途驱逐继续在模拟器中
  自愈，只是不再发出新的驱逐——与真实 `kubectl drain` 被中断后的安全姿态一致。
- **规划预测 vs 运行强制**：计划用"替代 Pod 立即就绪"的影子模拟器预测完整波次
  结构（哪些 Pod 同时可准入、顺序如何）；真实就绪等待由执行器的硬屏障在实时
  快照上强制。因此"替代 Pod 永不就绪"不会污染预测，也不会被谎报完成。

---

## 7. 运行测试与检查

```bash
python3 -m pytest -q                 # 60 passed
python3 scripts/demo.py              # 四个验收场景
python3 -m compileall -q app scripts # 语法编译检查
```

所有计算（副本/PDB/准入）、协议（FastAPI 路由、HMAC 令牌、状态机）与密码操作
（HMAC-SHA256、SHA-256）均为真实执行；失败会如实返回错误码与原因，不做假成功。
