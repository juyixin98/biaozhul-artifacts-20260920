# Offline NetworkPolicy Reachability Analyzer

纯后端的 Kubernetes NetworkPolicy 离线可达性分析器（Python + FastAPI）。
给定一份**不可变**的集群快照（命名空间、Pod、NetworkPolicy）和一条五元组
查询（源、目标、协议、端口），分别计算**源 egress** 与**目标 ingress**，
只有两侧同时允许时流量才可达；多个策略的允许集合取**并集**。

不做任何真实网络调用、不依赖集群——所有判定都在本地从快照推导。

## 实现的语义

| 主题 | 行为 |
| --- | --- |
| 默认策略 | Pod 在某方向上**没有任何**选中它的策略 => 该方向不隔离，全通；被隔离且无规则匹配 => 默认拒绝 |
| 双向门控 | `reachable = 源 egress 允许 AND 目标 ingress 允许`，结果中分别给出两侧证据 |
| 多策略并集 | 任意一条选中策略的任意一条规则允许即允许（union，不是交集） |
| `podSelector` | 真正的标签选择器：`matchLabels`（等值合取）+ `matchExpressions`（`In/NotIn/Exists/DoesNotExist`），**不是**名称/名字符串匹配 |
| `namespaceSelector` | 对命名空间标签做选择器求值；`namespaceSelector: {}` 匹配所有命名空间 |
| Peer 默认值 | `podSelector` 缺失 => 默认策略所在命名空间；`namespaceSelector` 缺失 => 同样默认本命名空间；空 peer `{}` 只匹配策略本命名空间 Pod；规则级 `from`/`to` **缺失**匹配所有端点（含外部 IP），`from: []` 不匹配任何人 |
| `ipBlock` | 用标准库 `ipaddress` 做真实 CIDR 成员判定（IPv4/IPv6），支持 `except` 排除；不能与 pod/namespace selector 混用；也可用对端 Pod IP 命中 |
| 端口 | 数值端口、`endPort` 连续范围（含端点）、命名端口（针对**目标** Pod 的 `containerPorts` 解析，且按协议区分）；`ports` 缺失 => 所有端口 |
| 协议 | 真实支持 TCP、UDP；SCTP 等未知协议查询直接返回 `422 unsupported_protocol`，绝不猜测放行；策略中出现 SCTP 端口会在快照上传时给出 warning |
| 空 vs 缺失 | 空选择器 `{}`（匹配全部）与字段缺失（`None`，触发默认语义）全程区分；显式 `policyTypes: []` 不隔离任何方向；`policyTypes` 缺失时按 Kubernetes 规则默认（Ingress 总是包含，Egress 仅在存在 egress 规则时包含） |
| 不可变快照 | 模型为冻结的 pydantic 对象；快照以内容的 **SHA-256**（`hashlib` 真实计算，canonical JSON）为 ID，重复上传幂等返回同一 ID |
| 证据 | 每条允许都返回 `policy(namespace/name)`、方向、规则下标、peer 下标、peer 类型与命中端口 |

## 目录结构

```
app/
  models.py      # 不可变领域模型 + 输入校验（pydantic v2）
  selectors.py   # 标签选择器求值（集合语义，非字符串匹配）
  analyzer.py    # 可达性引擎：隔离判定、peer/端口匹配、双向合成
  storage.py     # 内存不可变快照库，SHA-256 内容寻址
  main.py        # FastAPI 路由
  cli.py         # 批量离线分析 CLI（无需启动服务）
examples/
  scenario.json  # 含 3 命名空间、4 Pod、4 策略、7 条查询的示例
scripts/
  acceptance_queries.py  # 对运行中的服务跑真实断言
tests/           # 47 个自动化测试（选择器/引擎/API）
requirements.txt # 锁定依赖（pip freeze 产出）
acceptance.sh    # 一键验收脚本
```

## 本地启动

需要 Python 3.11+（开发环境为 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P088/a
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 安装锁定依赖
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8791
```

健康检查：

```bash
curl -s http://127.0.0.1:8791/health
# {"status":"ok","version":"1.0.0"}
```

> 存储是进程内的，多 worker 部署时请用单个 uvicorn worker（默认即如此）。

## API

### 1) 上传不可变快照

`POST /api/v1/snapshots`，请求体即快照 JSON。返回 SHA-256 内容 ID；
重复上传相同内容返回同一 ID 且 `created: false`。

```bash
SID=$(curl -s -X POST http://127.0.0.1:8791/api/v1/snapshots \
  -H 'Content-Type: application/json' \
  -d @<(python3 -c "import json;print(json.dumps(json.load(open('examples/scenario.json'))['snapshot']))") \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['snapshotId'])")
echo "$SID"
```

其他端点：`GET /api/v1/snapshots`（列出 ID）、`GET /api/v1/snapshots/{id}`（元数据）。

### 2) 发起可达性查询

`POST /api/v1/snapshots/{id}/analyze`

```json
{
  "source":      {"pod": {"namespace": "frontend", "name": "web-1"}},
  "destination": {"pod": {"namespace": "backend", "name": "api-1"}},
  "protocol": "TCP",
  "port": 9090
}
```

- 端点二选一：`{"pod": {"namespace": "...", "name": "..."}}` 或 `{"ip": "10.2.44.20"}`
- `port` 省略表示任意端口；`protocol` 只接受 `TCP`/`UDP`（其他返回 422）

响应（节选）：

```json
{
  "reachable": true,
  "reason": "egress and ingress both allow the traffic",
  "egress":  { "isolated": true, "allowed": true, "selectingPolicies": [...], "allowedBy": [...] },
  "ingress": { "isolated": true, "allowed": true, "selectingPolicies": [...],
               "allowedBy": [{
                 "policy": {"namespace": "backend", "name": "api-ingress"},
                 "direction": "ingress", "ruleIndex": 0, "peerIndex": 0,
                 "peer": "selectors", "protocol": "TCP",
                 "allowedPorts": "named:grpc" }] }
}
```

拒绝时 `reachable: false`，并在 `reason` 标明是源 egress 还是目标 ingress 拒绝；
`allowedBy: []` 表示该侧被隔离但没有任何规则放行（默认拒绝的直接证据）。

## 不用服务的批量 CLI

```bash
.venv/bin/python -m app.cli examples/scenario.json
# 退出码：0 全部可达/正常；1 存在校验或不支持协议错误；2（--fail-on-deny）存在拒绝
```

## 快照 JSON 形状

```json
{
  "namespaces": [{"name": "backend", "labels": {"tier": "api"}}],
  "pods": [{
    "name": "api-1", "namespace": "backend",
    "labels": {"app": "api"}, "ips": ["10.2.0.7"],
    "containerPorts": [{"name": "grpc", "containerPort": 9090, "protocol": "TCP"}]
  }],
  "policies": [{
    "name": "api-ingress", "namespace": "backend",
    "podSelector": {"matchLabels": {"app": "api"}},
    "policyTypes": ["Ingress", "Egress"],
    "ingress": [{
      "from": [{"namespaceSelector": {"matchLabels": {"tier": "web"}},
                "podSelector": {"matchLabels": {"app": "web"}}}],
      "ports": [{"port": "grpc", "protocol": "TCP"},
                {"port": 9100, "endPort": 9200, "protocol": "TCP"}]
    }],
    "egress": [{
      "to": [{"ipBlock": {"cidr": "10.2.44.0/24", "except": ["10.2.44.10/32"]}}]
    }]
  }]
}
```

## 验收命令

一键完成：建虚拟环境 → 安装**锁定**依赖 → 跑全部自动化测试 → 启动服务 →
对示例快照执行允许/拒绝/命名端口/ipBlock 排除/命名空间标签变化/未知协议等断言：

```bash
cd /home/admin/Downloads/biaozhul/P088/a
chmod +x acceptance.sh
./acceptance.sh
```

仅跑测试：

```bash
.venv/bin/python -m pytest tests/ -v
```

## 测试覆盖要点

- **默认拒绝**：显式 `policyTypes` + 空规则、双向隔离、单侧拒绝阻断
- **双向门控与并集**：egress 放行但 ingress 拒绝不可达；多策略多来源取并集
- **命名端口**：按目标容器端口解析（不被来源同名端口误导）、协议区分、缺失不放行、`endPort` 范围含端点
- **命名空间标签变化**：上传标签变更后的新快照，原来可达的流变为拒绝（不可变快照 => 新内容新 ID）
- **ipBlock 排除**：CIDR 内允许、`except` 排除（IPv4/IPv6）、对端 Pod IP 命中、非法 CIDR 拒绝
- **空选择器 vs 缺失字段**：`{}`、`policyTypes: []`、缺失 `policyTypes`、缺失/空 `from`/`to`
- **未知协议**：SCTP/自定义协议查询返回 `unsupported_protocol`，策略中的 SCTP 不产生 TCP/UDP 放行
- **防字符串匹配**：前缀值、近似键名（`app` vs `application`）、名称子串均不命中
- **真实密码学**：API 层断言快照 ID 等于 canonical JSON 的真实 SHA-256，重复上传幂等
