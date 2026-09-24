# NetworkPolicy 可达性分析器（离线，纯后端）

基于 Python + FastAPI 的离线 Kubernetes NetworkPolicy 可达性分析器。加载一份不可变的集群快照（namespace / pod / NetworkPolicy），对「源 Pod → 目标 Pod + 协议 + 端口」的查询分别计算**源侧 egress** 与**目标侧 ingress**，两侧同时允许才判定可达，并输出命中策略的证据。

## 语义规则

- **隔离（isolation）**：只要存在一条选中某 Pod 且治理该方向的策略，该 Pod 在该方向即被隔离；未被隔离的方向默认全通。
- **并集**：Pod 被隔离时，允许集合是所有选中它的策略在该方向上规则的**并集**。
- **空选择器 vs 缺失字段**：
  - `podSelector: {}` 选中策略所在 namespace 的**全部** Pod；
  - peer 中 `namespaceSelector: {}` 匹配所有 namespace；
  - peer 只有 `podSelector`（缺失 `namespaceSelector`）时，只匹配**策略自身 namespace** 内的 Pod；
  - `ingress: []`（字段存在但为空）= 隔离并全拒；字段缺失 = 该策略不治理此方向。
- **policyTypes 缺省推断**：未指定时总是治理 Ingress；仅当 `egress` 字段存在时才治理 Egress。
- **ipBlock**：按 CIDR 匹配 Pod IP，`except` 列表优先排除（`ipaddress` 模块真实计算）。
- **端口**：规则缺省 `ports` = 该规则放行所有端口；支持数字端口、`endPort` 区间、命名端口（按**目标 Pod** 声明的 containerPort 解析，协议须一致）。
- **协议**：仅支持 TCP / UDP（大小写不敏感）；其他协议（如 SCTP）返回 `unsupported_protocol`，不做猜测性分析。
- **不可变性**：快照解析为 frozen pydantic 模型，加载后任何字段都无法被修改；每次分析结果附带快照的 SHA-256 摘要。
- 选择器匹配全部基于标签字典的结构化求值（`matchLabels` + `matchExpressions` 的 In/NotIn/Exists/DoesNotExist），不做字符串匹配。

## 目录结构

```
app/
  models.py     # 不可变快照/查询模型（frozen pydantic）
  analyzer.py   # 选择器求值、peer/端口匹配、双向可达性分析
  main.py       # FastAPI 接口
examples/
  snapshot.json # 示例集群快照
tests/
  test_analyzer.py  # 核心语义测试（默认拒绝、命名端口、ns 标签变化、ipBlock 排除等）
  test_api.py       # API 端到端测试
requirements.txt    # 锁定依赖
```

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

## 验收命令

运行自动化测试（29 个用例）：

```bash
.venv/bin/python -m pytest tests/ -q
```

端到端手工验收：

```bash
# 1. 加载示例快照
curl -s -X PUT http://127.0.0.1:8000/snapshot \
  -H 'Content-Type: application/json' -d @examples/snapshot.json

# 2. web-1 -> api-1 命名端口 "http"（应可达，证据命中 backend/allow-web-to-api）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' -d '{
  "source": {"namespace": "frontend", "name": "web-1"},
  "destination": {"namespace": "backend", "name": "api-1"},
  "protocol": "TCP", "port": "http"}'

# 3. api-1 -> prom-1:9090（ipBlock except 排除 192.168.1.10，egress 拒绝，不可达）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' -d '{
  "source": {"namespace": "backend", "name": "api-1"},
  "destination": {"namespace": "monitoring", "name": "prom-1"},
  "protocol": "TCP", "port": 9090}'

# 4. 未知协议（返回 unsupported_protocol）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' -d '{
  "source": {"namespace": "frontend", "name": "web-1"},
  "destination": {"namespace": "backend", "name": "api-1"},
  "protocol": "SCTP", "port": 8080}'
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| PUT | `/snapshot` | 加载/替换集群快照，返回条目计数与 SHA-256 |
| GET | `/snapshot` | 当前快照摘要（未加载返回 409） |
| POST | `/analyze` | 可达性查询（未加载快照返回 409；Pod 不存在返回 422） |

### 查询输入

```json
{
  "source":      {"namespace": "frontend", "name": "web-1"},
  "destination": {"namespace": "backend",  "name": "api-1"},
  "protocol":    "TCP",
  "port":        8080
}
```

`port` 可为数字或命名端口字符串。响应包含 `reachable`、`egress` / `ingress` 两侧的 `isolated` / `allowed` / `evidence`（命中策略的 `namespace/name`、规则与 peer 下标）、`explanation` 以及 `snapshotSha256`。`status` 为 `ok` / `unsupported_protocol` / `unresolved_named_port`。
