# QoS 兼容诊断服务（ROS 2 + rclpy + FastAPI）

纯后端服务：通过 **真实 DDS 发现层**采集本机 ROS 2 节点的发布/订阅端点，分析每个
话题上 publisher/subscription 的 `reliability`、`durability`、`history`、`depth`，
明确区分**确定不兼容（DDS 拒绝建链）**与**仅性能风险（可连通但可能丢消息/耗内存）**，
并给出逐条可解释匹配链路、带时间戳的端点生命周期、以及带规则版本和密码学完整性保护的
拓扑快照。

> 不使用静态 JSON 充当演示证据：验收用真实 `rclpy` 发布/订阅节点制造可靠性、持久性
> 不兼容，再修改配置验证恢复，并用订阅端**实际收到的消息数**作为独立物理证据。

---

## 1. 环境要求

| 组件 | 版本 | 来源 |
|---|---|---|
| ROS 2 | Jazzy（实测） | 系统安装 `/opt/ros/jazzy`，默认中间件 rmw_fastrtps_cpp (Fast DDS) |
| Python | 3.12（≥3.10 即可） | 系统 |
| rclpy / std_msgs | 随 ROS 2 提供 | **不要 pip 安装** |
| FastAPI / uvicorn / pydantic / pytest / httpx | 见 `requirements.txt`（已锁定） | pip |

## 2. 安装与启动

```bash
cd qos_diag
source /opt/ros/jazzy/setup.bash          # 必须：提供 rclpy
python3 -m pip install -r requirements.txt   # 依赖已精确锁定
python3 -m qos_diag.cli serve --host 127.0.0.1 --port 8000
# 或安装后：pip install -e . && qosdiag serve
```

另开终端，启动真实发布/订阅节点（默认 QoS 相同，应判 COMPATIBLE）：

```bash
source /opt/ros/jazzy/setup.bash
python3 scripts/qos_talker.py
python3 scripts/qos_listener.py --status-file /tmp/listener.json
```

然后查看诊断：

```bash
curl -s http://127.0.0.1:8000/api/diagnose | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/topology | python3 -m json.tool
curl -s "http://127.0.0.1:8000/api/events?limit=50" | python3 -m json.tool
curl -s -XPOST http://127.0.0.1:8000/api/snapshots | python3 -m json.tool
```

> 建议把诊断节点与被测节点放在同一 `ROS_DOMAIN_ID`（默认 0 即可）。

## 3. 一键验收（真实节点 + 真实收发证据）

```bash
source /opt/ros/jazzy/setup.bash
python3 scripts/acceptance.py
```

脚本会自动：随机 `ROS_DOMAIN_ID` 隔离 → 后台启动 HTTP 服务 →

1. **可靠性不兼容**：listener `reliable` × talker `best_effort`
   → API 判 `INCOMPATIBLE / R-REL-001`，且 listener 4 秒内实际收到 **0** 条消息；
   把 talker 修正为 `reliable` → API 恢复、listener 实际开始收到消息。
2. **持久性不兼容**：listener `transient_local` × talker `volatile`
   → `INCOMPATIBLE / R-DUR-001`，实际收到 0 条；修正 talker 为 `transient_local`
   → 恢复并实际收到消息。
3. **快照**：创建/读取成功（含规则版本），篡改一个字节后再读必须被 **409** 拒绝
   （SHA-256 + HMAC-SHA256 真实校验）。

全部通过时退出码 0，任一不符退出码 1；节点日志保留在输出的临时目录中。

## 4. 自动化测试

```bash
source /opt/ros/jazzy/setup.bash
python3 -m pytest -q          # 34 个测试
```

| 文件 | 内容 | 是否需要 ROS |
|---|---|---|
| `tests/test_rules.py` | 规则引擎：不兼容/风险/通过、证据不足、示例拓扑 | 否 |
| `tests/test_store.py` | SHA-256/HMAC-SHA256 真实计算、篡改与换钥检测 | 否 |
| `tests/test_api.py` | FastAPI 全部端点（注入静态拓扑） | 否 |
| `tests/test_realtime_ros.py` | 真实 Fast DDS 发现：两类不兼容、恢复、depth 仅风险、端点生命周期宽限期 | 是 |

没有 source ROS 时，最后一个文件自动 skip，其余测试照常运行（适合纯 CI）。

离线规则演示（不需要 ROS、不起节点）：

```bash
python3 -m qos_diag.cli analyze examples/sample_topology.json   # 含不兼容时退出码 2
```

## 5. HTTP API

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /api/rules` | 规则目录、严重级别、规范依据、规则版本 `qos-rules-1.0.0` |
| `GET /api/topology` | 当前全部端点及其 QoS、状态、首末见时间 |
| `GET /api/events?topic=&limit=` | `discovered/rediscovered/lost` 事件（均带时间戳） |
| `GET /api/diagnose?topic=` | 话题级诊断：配对、逐条 finding 与 `chain` 匹配链路 |
| `POST /api/snapshots` | 保存当前拓扑快照（含规则版本） |
| `GET /api/snapshots` / `GET /api/snapshots/{file}` | 快照列表 / 读取（读时强制完整性校验） |

话题级 `verdict` 取值：

- `incompatible`：存在确定不兼容的配对（DDS 不会建立数据通路）；
- `risk`：可连通，但有性能/可观测性风险；
- `compatible`：全部检查通过；
- `insufficient_evidence`：发布或订阅一侧尚未发现——**不臆断为故障**。

## 6. 判定规则（版本 `qos-rules-1.0.0`）

QoS 兼容遵循 DDS“订阅方请求、发布方提供”的模型：

| 规则 | 维度 | 结论 | 依据 |
|---|---|---|---|
| R-REL-001 | reliability | sub=reliable 且 pub=best_effort → **不兼容** | 请求可靠而发布方只能尽力而为，DDS 拒绝连接 |
| R-DUR-001 | durability | sub=transient_local 且 pub=volatile → **不兼容** | 请求 latched 历史而发布方不保留，DDS 拒绝连接 |
| R-TYPE-001 | topic type | 类型名不一致 → **不兼容** | 无法建立数据通路 |
| R-HIST-001 | history | KEEP_LAST/KEEP_ALL 差异 → **仅风险** | 非线上协商维度，只改变缓存/丢弃行为 |
| R-DEPTH-001 | depth | KEEP_LAST 下 depth 差异 → **仅风险** | 只影响本端队列长度，不影响建链 |
| R-DISC-001 | 发现状态 | 端点在宽限期内暂未发现 → **仅风险** | 短暂未发现 ≠ 永久故障 |
| R-UNKNOWN-001 | 未知取值 | QoS 回报 unknown/system_default → **仅风险** | 无法静态确认，提示显式配置 |

每条 finding 都带 `chain`：`publisher_value` / `subscription_value` / `expected` /
`detail`，完整展示“取值 → 规则 → 结论”的推理链；通过项也输出 `severity=ok`。

**实测注意（真实行为，非简化）**：Fast DDS 的线上发现只交换 reliability 与
durability，`history/depth` 对远端回报 `UNKNOWN/0`（`ros2 topic info -v` 同样如此）。
因此实时模式下 history/depth 维度会给出 R-UNKNOWN-001 风险提示；R-HIST-001/R-DEPTH-001
的差异分析在离线拓扑（如 `examples/sample_topology.json`）上完整生效。

## 7. 端点生命周期与“短暂未发现”

```
(new) discovered ──本轮未发现──▶ suspected_missing（保留 last-known QoS，记 missing_since）
                   ◀─同 GID 重新发现──   │ 持续缺失超过 missing_grace_seconds（默认 5s）
                   rediscovered         ▼
                                        lost（确认消失，发出 lost 事件）
```

- `suspected_missing` 端点仍按最后已知 QoS 参与诊断，但会附加 R-DISC-001 风险；
- 宽限期内同一 GID 恢复 → `rediscovered`；DDS 实体重建会分配**新 GID**，
  此时视为全新 `discovered` 端点，旧 GID 超期后才 `lost`；
- 所有状态转换均带 Unix 时间戳，可在 `/api/events` 审计。

## 8. 快照完整性（真实密码学运算）

落盘快照是一个信封：`payload`（规范化 JSON，`sort_keys`）+ 真实计算的
`SHA-256(payload)` 与 `HMAC-SHA256(key, payload)`。

- 读取时两个值都用 `hmac.compare_digest` 重新计算比对：改内容 → SHA 不符（409）；
  即使重算 SHA，没有密钥也过不了 HMAC；换密钥签名同样被拒。
- 密钥解析：环境变量 `QOSDIAG_HMAC_KEY` → `data/.hmac_key`（首次自动生成，权限 0600）
  → 仅本地开发默认值。生产环境请设置 `QOSDIAG_HMAC_KEY`。
- 快照内固定保存 `rules_version`，诊断规则升级后仍可按旧版本解释历史结论。

## 9. 目录结构

```
qos_diag/
├── qos_diag/            # 服务代码
│   ├── models.py        # 数据模型（不依赖 rclpy）
│   ├── rules.py         # 兼容规则引擎 + 可解释链路（不依赖 rclpy）
│   ├── collector.py     # rclpy 真实 DDS 发现 + 端点生命周期
│   ├── store.py         # 快照持久化 + SHA-256/HMAC-SHA256
│   ├── service.py       # FastAPI
│   └── cli.py           # serve / snapshot / analyze
├── scripts/
│   ├── qos_talker.py    # 真实发布节点（QoS 全可配）
│   ├── qos_listener.py  # 真实订阅节点（输出实际收消息计数）
│   └── acceptance.py    # 端到端验收
├── tests/               # 34 个自动化测试
├── examples/sample_topology.json
├── requirements.txt     # 锁定依赖
└── pyproject.toml
```
