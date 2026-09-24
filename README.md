# 拓扑调度候选分析（Topology Scheduling Candidate Analysis）

纯后端、**离线**的 Kubernetes 调度候选分析服务（Python + FastAPI）。给定一组节点、
存量 Pod 和一个候选 Pod，先过滤硬约束、再对可行节点做软约束评分，输出**每个节点**
的淘汰原因（含证据）或评分明细，平局按节点名升序。

资源一律按 **requests** 计算（不用实时利用率），硬约束与软约束严格分离：
软约束**永远不会**挽救硬约束失败的节点。

> 范围声明：本项目只实现 Kubernetes 调度器的一个**明确子集**，用于离线分析与教学。
> 它**不**连接任何生产集群，**不**读取实时利用率，**不**声称与默认调度器
> （kube-scheduler）逐位等价。未实现项会在响应的 `notImplemented` 字段中如实列出。

## 实现的调度子集

### 硬约束（Filter，任一失败即淘汰，按固定顺序报告首个失败原因）

1. **资源适配**：候选 Pod 的 requests ≤ 节点 allocatable（缺省取 capacity）减去
   该节点上存量 Pod requests 之和。逐资源给出 requested/allocatable/alreadyUsed/available。
   CPU 支持核数与 `m`（毫核），内存支持 `Ki/Mi/Gi/...` 与 `k/M/G/...`，扩展资源（如
   `nvidia.com/gpu`）必须为非负整数。
2. **污点与容忍度**：仅 `NoSchedule` / `NoExecute` 是硬约束；支持 `Equal`、`Exists`
   及空 key 通配容忍、按 effect 限定。
3. **pod.nodeSelector**。
4. **required node affinity**（term 间 OR，term 内 AND；In/NotIn/Exists/DoesNotExist）。
5. **required pod affinity**（按 topologyKey 域查找匹配存量 Pod；命名空间缺省同候选 Pod）。
6. **required pod anti-affinity**（同域内出现匹配存量 Pod 即冲突，同节点也在同一域内）。
7. **DoNotSchedule 拓扑分布**：放置后本域计数 + 1 与全局最小域计数之差不得超过 maxSkew。
   缺少 topologyKey 标签的节点以 `MissingTopologyLabel` 淘汰。

### 软约束（Score，只对通过全部硬约束的节点；0–100，按权重平均）

- `leastAllocated`：放置后各资源剩余比例的平均（越多空闲分越高）。
- `preferredNodeAffinity`：命中的 preferred node term 的最大 weight。
- `preferredPodAffinity` / preferred anti-affinity：命中的最大 weight。
- `taintPreference`：每有一个未被容忍的 `PreferNoSchedule` 污点减 10 分（下限 0）。
- `topologySpread`：`ScheduleAnyway` 与硬分布约束按
  `100 * (1 - skew/(maxSkew+1))` 给分。

权重可通过请求体 `scoringWeights` 调整（默认均为 1.0；未配置的评分器标记
`applicable=false` 且不参与平均，不会拉低总分）。

**已知简化（与上游不同，勿当作默认调度器）**：评分公式是文档化的近似；无 Volume
绑定、优先级/抢占、PodDisruption、设备插件、镜像本地性等；不模拟调度队列与多 Pod
联动，分析的是单个候选 Pod 的静态快照。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/api/v1/info` | 范围、版本、已实现特性 |
| POST | `/api/v1/analyze` | 提交分析请求，返回逐节点结论与 HMAC 签名 |
| POST | `/api/v1/verify-signature` | 用共享密钥校验响应体签名 |

交互文档：启动后访问 `http://127.0.0.1:8000/docs`（Swagger UI）。

### 请求/响应

JSON 字段为 **camelCase**（如 `topologySpreadConstraints`、`whenUnsatisfiable`）。
未知字段一律 422 拒绝，避免拼写错误静默关闭某个约束。完整示例见
[`examples/full_request.json`](examples/full_request.json)，响应形状：

```jsonc
{
  "scope": "offline-kubernetes-scheduler-subset/v1",
  "pod": "api-7",
  "namespace": "prod",
  "nodes": [
    {
      "node": "node-aaa",
      "feasible": true,
      "rejectionReason": null,
      "evidence": null,
      "score": {
        "totalScore": 60.0,
        "components": [
          {"scorer": "leastAllocated", "score": 80, "applicable": true, "weight": 1.0}
        ]
      }
    },
    {
      "node": "node-ccc",
      "feasible": false,
      "rejectionReason": "TaintNotTolerated",
      "evidence": {"taint": {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}}
    }
  ],
  "ranking": [{"node": "node-aaa", "score": 60.0, "rank": 1}],
  "selectedNode": "node-aaa",
  "signature": "<hex hmac-sha256>",
  "hmacAlgorithm": "HMAC-SHA256"
}
```

淘汰原因码：`InsufficientResources`、`TaintNotTolerated`、`NodeSelectorNotMatch`、
`NodeAffinityNotMatch`、`PodAffinityConflict`、`PodAntiAffinityConflict`、
`TopologySpreadSkew`、`MissingTopologyLabel`。

### 响应完整性签名（真实 HMAC-SHA256）

每个 `/analyze` 响应对「去掉 `signature`/`hmacAlgorithm` 后的规范 JSON
（键排序、紧凑分隔）」用 **HMAC-SHA256** 计算十六进制摘要。密钥取自环境变量
`TOPOLOGY_SCHED_HMAC_KEY`；未设置时使用仅用于本地开发的默认密钥
（`app/crypto.py` 中的 `DEV_DEFAULT_KEY`，切勿用于生产）。客户端可用
`/api/v1/verify-signature` 或自行用同一规范重算。

## 本地启动

需要 Python 3.11+（开发于 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 安装锁定依赖（含 pytest/httpx）
TOPOLOGY_SCHED_HMAC_KEY=some-secret \
  .venv/bin/python -m uvicorn app.main:app --reload
```

- `requirements.txt`：完整锁定（pip freeze，含传递依赖与精确版本）。
- `requirements.in`：直接依赖的顶层声明。

## 验收命令

```bash
# 1) 自动化测试（26 个用例：数量解析、选择器、容忍度、四类夹具、软硬约束分离、HTTP 与 HMAC）
.venv/bin/python -m pytest -q

# 2) 启动服务
.venv/bin/python -m uvicorn app.main:app --port 8000 &

# 3) 用完整示例做一次分析
curl -s -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/full_request.json | python3 -m json.tool

# 4) 健康检查
curl -s http://127.0.0.1:8000/healthz
```

四类必需夹具位于 [`tests/fixtures/`](tests/fixtures/)，每个夹具在
`tests/test_fixtures.py` 中逐项断言：

| 夹具 | 场景 |
| --- | --- |
| `insufficient_resources.json` | 资源不足：CPU 剩余 500m 放不下 1 核请求 |
| `anti_affinity_conflict.json` | 反亲和冲突：同 zone 已有匹配 Pod |
| `zone_skew.json` / `zone_skew_all_rejected.json` | zone 偏斜：单约束向最空域再平衡；zone+rack 双硬约束交集为空导致全部淘汰 |
| `toleration_match.json` | 容忍度：污点值不匹配淘汰；匹配通过；PreferNoSchedule 仅扣分 |

## 项目结构

```
app/
  main.py        FastAPI 路由与错误处理
  models.py      Pydantic v2 协议模型（camelCase，extra=forbid）
  quantities.py  CPU/内存/扩展资源数量解析（Decimal 精确比较）
  selectors.py   标签选择器（In/NotIn/Exists/DoesNotExist）
  filters.py     硬约束过滤（含污点匹配、拓扑分布偏斜计算）
  scoring.py     软约束评分与加权
  analyzer.py    编排：过滤 → 评分 → 排序（平局按节点名）
  crypto.py      HMAC-SHA256 签名/校验（标准库真实计算）
examples/        示例请求
tests/           pytest 用例与夹具
```
