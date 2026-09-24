# 拓扑调度候选分析（topology-scheduler-analyzer）

离线 Kubernetes 调度候选分析 API。给定节点资源/污点、待调度 Pod 的 request 与
亲和约束、拓扑分布规则，对候选节点**先硬约束过滤、再软约束评分**，输出每个节点
被淘汰的原因或得分明细，平局按节点名字典序，给出最终胜出节点。

**纯后端、纯离线**：不连接任何真实集群，不读取任何实时指标。资源一律按
**request** 求和计算，绝不使用实时利用率。

## 明确的能力边界（不声称的部分）

这是 kube-scheduler 默认行为的**明确子集**，不是完整调度器：

- 不实现调度队列、抢占（preemption）、NoExecute 驱逐计时
- 不实现 volume 约束、端口冲突、nodeName 指定、DaemonSet 等特殊路径
- `topologySpreadConstraints` 只实现**软版本**（zone 偏斜评分），不实现
  `maxSkew`/`DoNotSchedule` 硬语义
- 反亲和只检查"待调度 Pod 自己的反亲和条款 vs 已有 Pod"，不做反向检查
- 资源维度只覆盖 `cpu` 与 `memory`

## 硬约束（Filter，任一失败即淘汰并给出原因）

1. `node_selector`：节点标签逐键匹配
2. `node_affinity.required_terms`：term 内 AND、term 间 OR，支持
   `In/NotIn/Exists/DoesNotExist/Gt/Lt`
3. 污点：`NoSchedule` 与 `NoExecute` 污点必须被 toleration 容忍
   （`Equal`/`Exists`，effect 为空时匹配所有 effect）
4. 资源：`pod.request + 节点上已有 Pod 的 request 之和 <= allocatable`
   （cpu 毫核、内存字节分别校验，恰好相等视为可行）
5. `anti_affinity.required_terms`：同一 `topology_key` 域内已有
   `match_labels` 命中的 Pod 即冲突；节点缺少该 topology key 也判失败

## 软约束（Score，各插件 0–100，加权平均为 final_score）

| 插件 | 含义 |
|---|---|
| `least_allocated` | 放入后剩余 request 容量占比（cpu/内存平均），越空闲越高分 |
| `zone_spread` | `topology_spread.match_labels` 命中的 Pod 在各拓扑域的分布，所在域越空越高分 |
| `preferred_node_affinity` | preferred 节点亲和条款按 weight 命中的比例 |
| `preferred_anti_affinity` | preferred 反亲和条款未被违反的比例 |
| `prefer_no_schedule_taint` | 存在未容忍的 `PreferNoSchedule` 污点则 0 分，否则 100 |

软约束**只影响评分，绝不淘汰节点**。各插件权重由请求体 `weights` 控制
（默认全 1，0 表示禁用）。排名按 `final_score` 降序，**平局按节点名字典序**，
保证结果确定性。

## 目录结构

```
app/
  quantity.py    # K8s 资源量解析（500m / 128Mi / 1.5 等，十进制+二进制 SI 子集）
  models.py      # Pydantic 请求/响应模型（含资源量校验）
  scheduler.py   # 过滤 + 评分引擎
  main.py        # FastAPI 入口（GET /healthz, POST /v1/analyze）
examples/           # 四个示例夹具（可直接 POST）
tests/           # pytest 自动化测试（38 项）
requirements.txt # 锁定依赖（pip freeze 生成）
```

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --port 8000
```

## 验收命令

```bash
# 1. 自动化测试（应全部通过）
.venv/bin/python -m pytest -q

# 2. 健康检查
curl -s http://127.0.0.1:8000/healthz

# 3. 回放四个夹具（资源不足 / 反亲和冲突 / zone 偏斜 / 容忍度匹配）
for f in examples/*.json; do
  echo "== $f"
  curl -s -X POST http://127.0.0.1:8000/v1/analyze \
    -H 'Content-Type: application/json' -d @"$f" | python3 -m json.tool
done
```

各夹具预期结果：

| 夹具 | winner | 被淘汰节点 |
|---|---|---|
| `insufficient_resources.json` | `node-big` | `node-small`（cpu request 之和超 allocatable） |
| `anti_affinity_conflict.json` | `node-b` | `node-a`（同 hostname 已有 `app=web` Pod） |
| `zone_skew.json` | `node-b1` | 无（zone 偏斜是软约束，只拉低 zone-a 节点分数） |
| `toleration_match.json` | `node-normal` | `node-gpu`（未容忍 `dedicated=gpu:NoSchedule`） |

## API 摘要

`POST /v1/analyze` 请求体：

```json
{
  "pod": {"name": "p", "requests": {"cpu": "500m", "memory": "512Mi"},
          "node_selector": {}, "tolerations": [],
          "node_affinity": {"required_terms": [], "preferred_terms": []},
          "anti_affinity": {"required_terms": [], "preferred_terms": []},
          "topology_spread": {"topology_key": "topology.kubernetes.io/zone",
                              "match_labels": {"app": "web"}}},
  "nodes": [{"name": "n1", "labels": {}, "allocatable": {"cpu": "2", "memory": "2Gi"},
             "taints": []}],
  "existing_pods": [{"name": "e", "node_name": "n1", "labels": {},
                     "requests": {"cpu": "500m", "memory": "512Mi"}}],
  "weights": {"least_allocated": 1, "zone_spread": 1,
              "preferred_node_affinity": 1, "preferred_anti_affinity": 1,
              "prefer_no_schedule_taint": 1}
}
```

响应中每个节点给出 `feasible`、`filter_reasons`（淘汰原因，可多条）、
`scores`（各插件明细）、`final_score`、`rank`；顶层 `winner` 为第 1 名节点
（无可行节点时为 `null`）。交互式文档见启动后的 `/docs`。
