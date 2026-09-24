# ROS 2 QoS 兼容诊断服务（QoS Compatibility Diagnostics）

纯后端服务：采集本机 ROS 2 节点的发布/订阅端点，分析 **reliability / durability /
history / depth** 四项 QoS 策略，区分**确定不兼容（INCOMPATIBLE，DDS 拒绝连接）**
与**仅性能风险（RISK，能连通但可能丢消息/涨内存）**。使用 Python + rclpy + FastAPI。

- 端点发现/消失均带时间戳；短暂未发现进入宽限状态 **SUSPECT**，绝不立即判为永久故障；
  超过宽限期才 **EXPIRED**，重新发现回到 **ACTIVE** 并累计 `returns`。
- 拓扑快照带**规则版本 + 规则文件哈希 + 内容 SHA-256 + HMAC-SHA256**，可重算校验，
  能区分内容篡改、密钥伪造、规则版本漂移。
- 每个诊断结论给出**可解释匹配链路**：规则 ID → 双方取值 → 证据来源 → 判定 → 理由。
- 证据来源真实可溯：reliability/durability 来自 **DDS 发现（rmw）**；history/depth
  DDS 发现报文不传（Fast-DDS 实测为 UNKNOWN/0），通过真实 DDS 带外公告话题
  `/qos_diag/endpoints`（latched，RELIABLE+TRANSIENT_LOCAL）由节点周期上报补全。

> 运行环境：Ubuntu 24.04 + ROS 2 Jazzy（rmw_fastrtps_cpp），Python 3.12。
> 纯逻辑与 API 测试可在无 ROS 的机器上以 sim 模式运行。

---

## 1. 快速开始

```bash
# 1) 安装/锁定依赖（rclpy 由 ROS 提供，不在 pip 中）
python3 -m pip install -r requirements.txt

# 2) source ROS 2
source /opt/ros/jazzy/setup.bash

# 3) 启动服务（默认 0.0.0.0:8000；本文用 8077 示例）
python3 -m uvicorn qos_diag.api:app --host 127.0.0.1 --port 8077
```

健康检查：

```bash
curl -s localhost:8077/health | python3 -m json.tool
# {"status":"ok","mode":"ros2","rmw":"rmw_fastrtps_cpp", ...}
```

## 2. 一键验收（真实多进程节点，非静态 JSON）

自动启动服务、真实发布/订阅进程，分别制造 reliability 与 durability 不兼容、
修复并验证恢复、校验快照密码学、验证宽限期状态机，**全部断言通过才返回 0**：

```bash
source /opt/ros/jazzy/setup.bash
QOSDIAG_PORT=8077 python3 scripts/acceptance.py
```

预期输出（约 40 秒，含 Fast-DDS 默认 20s 参与者租约等待）：

```
Phase A: publisher BEST_EFFORT  vs subscriber RELIABLE         -> INCOMPATIBLE (R1)
Phase A fix: subscriber 重启为 BEST_EFFORT                      -> COMPATIBLE
Phase B: publisher VOLATILE     vs subscriber TRANSIENT_LOCAL  -> INCOMPATIBLE (R2)
Phase B fix: publisher 重启为 TRANSIENT_LOCAL                   -> RISK(R3)（能连通，仅性能风险）
snapshot : 内容 SHA-256 + HMAC-SHA256 + 规则哈希 全部校验通过
tamper   : 篡改后内容哈希与 HMAC 均失败（真实密码学）
grace    : SIGKILL 后租约到期 -> SUSPECT ->（过宽限期）EXPIRED -> 重新出现 ACTIVE(returns=1)
ALL ACCEPTANCE CHECKS PASSED
```

也可以接入**已在运行**的服务（不自动拉起）：

```bash
python3 scripts/acceptance.py --no-server   # 使用 QOSDIAG_BASE_URL
```

## 3. 手动制造 / 修复不兼容

终端 1（启动服务，见上）。终端 2、3：

```bash
source /opt/ros/jazzy/setup.bash

# —— 场景 A：reliability 不兼容 ——
# 发布者只提供 BEST_EFFORT
python3 scripts/demo_publisher.py    --name pub_a --topic /data \
    --reliability best_effort --durability volatile --history keep_last --depth 5
# 订阅者要求 RELIABLE（DDS 请求-提供模型：请求无法满足 => 拒绝匹配）
python3 scripts/demo_subscriber.py   --name sub_a --topic /data \
    --reliability reliable    --durability volatile --history keep_last --depth 10

curl -s localhost:8077/api/v1/diagnosis/data | python3 -m json.tool
# severity = INCOMPATIBLE, blocking_rules = ["R1"]

# 修复：把订阅者重启为 best_effort（Ctrl-C 后）
python3 scripts/demo_subscriber.py   --name sub_a --topic /data \
    --reliability best_effort --durability volatile --history keep_last --depth 10
# severity = COMPATIBLE
```

```bash
# —— 场景 B：durability 不兼容 ——
python3 scripts/demo_publisher.py    --name pub_b --topic /latched \
    --reliability reliable --durability volatile        --history keep_last --depth 10
python3 scripts/demo_subscriber.py   --name sub_b --topic /latched \
    --reliability reliable --durability transient_local --history keep_all  --depth 10
# severity = INCOMPATIBLE, blocking_rules = ["R2"]

# 修复：发布者重启为 TRANSIENT_LOCAL（提供 latched 历史）
python3 scripts/demo_publisher.py    --name pub_b --topic /latched \
    --reliability reliable --durability transient_local --history keep_last --depth 10
# 连通；订阅端 KEEP_ALL => severity = RISK, risk_rules = ["R3"]（仅性能风险）
```

## 4. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 状态、模式（ros2/sim）、RMW 标识、宽限期 |
| GET | `/api/v1/rules/version` | 规则版本、规则文件 SHA-256、规则清单 |
| GET | `/api/v1/topology?include_expired=true` | 全部端点及生命周期字段（时间戳/状态/证据） |
| GET | `/api/v1/diagnosis` | 所有话题的诊断 |
| GET | `/api/v1/diagnosis/{topic}` | 单话题完整匹配链路 |
| POST | `/api/v1/snapshots` | 立即保存签名快照 |
| GET | `/api/v1/snapshots` | 快照列表 |
| GET | `/api/v1/snapshots/{id}` | 快照原文 |
| GET | `/api/v1/snapshots/{id}/verify` | 重算内容哈希 / HMAC / 规则哈希 |

诊断 JSON 要点：

- `severity`：`INCOMPATIBLE` / `RISK` / `COMPATIBLE` / `PROVISIONAL` /
  `UNKNOWN` / `NO_PUBLISHER` / `NO_SUBSCRIBER`
- `matches[]`：**双方当前均 ACTIVE** 的确认匹配；每条含 `blocking_rules`、
  `risk_rules` 与完整 `steps[]` 匹配链路。
- `provisional_matches[]`：涉及宽限期内 SUSPECT 端点的匹配，只作参考，
  **不会**把话题结论升级成 INCOMPATIBLE。
- `publishers[]/subscriptions[]`：`state`、`qos`、`provenance`（每个属性的证据来源）、
  `rmw_observed`（DDS 线上原始值）、`announced`（公告值）、
  `first_seen_ts/last_seen_ts/last_announce_ts/absent_for_s/returns`。

示例：`examples/diagnosis_example.json`、`examples/announcement_example.json`。

## 5. 诊断规则（规则版本 `1.0.0`，见 `qos_diag/rules.py`）

DDS 采用 **request-offer（请求-提供）** 兼容模型：

| 规则 | 属性 | 判定 |
|---|---|---|
| **R1** | reliability | 订阅者 `RELIABLE` 且发布者 `BEST_EFFORT` → **INCOMPATIBLE**（请求可靠，提供方不保证）。反向可连通。 |
| **R2** | durability | 订阅者 `TRANSIENT_LOCAL` 且发布者 `VOLATILE` → **INCOMPATIBLE**（需要 latched 历史，提供方没有）。反向可连通。 |
| **R3** | history | 任一侧 `KEEP_ALL` → **RISK**：慢消费者无界缓冲，可能涨内存；不影响连通。 |
| **R4** | depth | `KEEP_LAST` 下订阅深度 < min(10, 发布深度) → **RISK**：突发超过消费速度会覆盖丢消息；不影响连通。 |

- `INCOMPATIBLE` 优先于 `RISK`；但所有规则都会执行并写入链路（即使已被阻断，
  风险也一并展示）。
- `SYSTEM_DEFAULT` 按 rmw 默认值解析（RELIABLE/VOLATILE/KEEP_LAST/depth=10），
  证据标为 `inferred_default`。
- 属性尚未观察到时给 `UNKNOWN` / `SKIP_UNKNOWN`，**不**判为不兼容。
- 只有双方都在最近一次 DDS 图中为 `ACTIVE` 时，才下确定不兼容结论。

## 6. 端点生命周期与“短暂未发现 ≠ 永久故障”

```
ACTIVE ──图中消失──────────────► SUSPECT ──持续消失 ≥ 宽限期──► EXPIRED
  ▲                                │                              │
  └────────── 重新发现（returns++）◄┴──────────────────────────────┘
```

- 优雅退出（`rclpy.shutdown()`，发出 DDS goodbye）：实测即时从图中消失。
- 强杀（SIGKILL，无 goodbye）：Fast-DDS 默认参与者租约 **20s** 后才撤销端点——
  这段时间它仍在图里；撤销后进入 SUSPECT。**这本身就是“不能把暂时没看到当故障”
  的一部分。**
- 宽限期默认 10s（`QOSDIAG_GRACE_PERIOD_S`，验收脚本用 3s）。SUSPECT 端点仍参与
  展示与参考性匹配，但不驱动确定结论。
- 同名节点重启拿到新 DDS GID 时，身份（namespace/node/topic/role）记录迁移，
  保留 `first_seen_ts`，新的 rmw 观测立即覆盖旧 QoS。

## 7. 快照完整性（真实密码学操作）

每个快照（`data/snapshots/<id>.json`）含：

- `rules_version` + `rules_hash`（生成时 `rules.py` 的 SHA-256，捕捉“版本号没变
  但规则已改”的情况）；
- `content_sha256`：body 规范化 JSON（`sort_keys`、固定分隔符）的 SHA-256；
- `hmac_sha256`：对**重算出的内容摘要**的 HMAC-SHA256（不是对存储字段签名，
  防止连哈希一起伪造）；密钥来自 `QOSDIAG_SNAPSHOT_KEY`，否则在
  `data/snapshots/.key`（0600）自动生成。

`/verify` 重算并分别报告三项结果。验收脚本真实篡改 body 落盘后验证：
内容哈希与 HMAC 同时失败、规则哈希不受影响；并有用错误密钥/伪造 MAC 失败的单测。

## 8. 架构与目录

```
qos_diag/
  qos_model.py    QoS 枚举/值对象/默认值/证据来源常量/规则版本
  rules.py        纯规则引擎：R1–R4、严重度、可解释匹配链路（无 ROS 依赖）
  topology.py     端点存储、GID↔身份双索引、公告融合、ACTIVE/SUSPECT/EXPIRED 状态机
  snapshot.py     快照落盘 + SHA-256/HMAC-SHA256/规则哈希与校验
  protocol.py     公告协议（/qos_diag/endpoints 上的 JSON-in-String）
  service.py      诊断服务：配对、CONFIRMED vs PROVISIONAL、API DTO
  api.py          FastAPI（后台线程跑 rclpy 监视器；无 rclpy 时降级 sim）
  nodes/
    __init__.py   DiagnosticsNode：普通节点 + 周期公告自身真实 QoS
    monitor.py    MonitorNode：轮询真实 ROS 图 + 收公告 + 推进生命周期
scripts/
  demo_publisher.py / demo_subscriber.py   真实、QoS 可配置的演示节点
  acceptance.py   端到端验收（真实多进程）
tests/            34 个自动化测试（规则/状态机/密码学/API sim/rclpy 真实 DDS）
examples/         公告与诊断 JSON 示例
```

数据流：DDS 图（rmw 发现，提供 GID+reliability+durability）＋公告话题（提供
history+depth）→ `TopologyStore` 融合（每个属性带 provenance）→ `DiagnosticsService`
配对判定 → FastAPI；快照写入带签名的 JSON。

## 9. 运行测试

```bash
# 无需 ROS：规则引擎、状态机、密码学、sim 模式 API（31 项）
python3 -m pytest tests/test_rules.py tests/test_topology.py \
                 tests/test_snapshot.py tests/test_api.py

# 需要 source ROS：真实 DDS 集成测试（3 项，真实节点 + 监视器 + 公告 + 状态机）
source /opt/ros/jazzy/setup.bash
python3 -m pytest tests/test_rclpy_integration.py -v

# 全部
python3 -m pytest
```

## 10. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QOSDIAG_GRACE_PERIOD_S` | `10` | 消失宽限期，超过才 EXPIRED |
| `QOSDIAG_POLL_PERIOD_S` | `0.5` | 监视器轮询 ROS 图周期 |
| `QOSDIAG_DATA_DIR` | `./data` | 快照目录根（`snapshots/` 在其下） |
| `QOSDIAG_SNAPSHOT_KEY` | 自动生成 | 快照 HMAC 密钥 |
| `QOSDIAG_SIM=1` | 关 | 不启动 rclpy，纯 sim（便于无 ROS 的 CI） |
| `QOSDIAG_PORT` / `QOSDIAG_BASE_URL` | `8077` | 验收脚本用 |

## 11. 实测到的关键事实（Jazzy + Fast-DDS）

1. `get_publishers_info_by_topic/get_subscriptions_info_by_topic` 跨进程发现约
   0.2s；返回的 QoS 中 **history=UNKNOWN、depth=0**——这两项不在 DDS 发现报文里，
   因此本项目用真实带外公告协议补全并明确标注证据来源，而不是假装能直接发现。
2. Jazzy 的 rclpy 不再暴露本地端点 GID 的 Python 接口，故公告以
   (namespace/node/topic/role) 标识，监视器用身份别名与 DDS GID 关联。
3. 优雅退出端点即时消失；SIGKILL 后约 20s（参与者租约）才从图中撤销。

这些都是真实执行测得，而非依据静态 JSON 的演示。
