# 请求样例

以下样例针对本地服务 `http://127.0.0.1:8000`。所有数据均为本地合成夹具。

## 1. 健康检查与夹具目录

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}

curl -s http://127.0.0.1:8000/fixtures
```

## 2. 用内置夹具创建会话

```bash
curl -s -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "fixture": "branching",
    "master_seed": 1234
  }'
```

响应包含 `session_id`、拓扑序、各节点形状以及共享节点（`shared`、`W3`）。

## 3. 规划检查点

无预算（在可行域内最小化额外重算）：

```bash
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/plan \
  -H 'Content-Type: application/json' \
  -d '{"rng_strategy": "counter"}'
```

带内存预算（单位：float64 元素）：

```bash
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/plan \
  -H 'Content-Type: application/json' \
  -d '{"rng_strategy": "counter", "memory_budget": 120}'
```

切换 RNG 重放策略 / 强制穷举或贪心：

```bash
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/plan \
  -H 'Content-Type: application/json' \
  -d '{"rng_strategy": "snapshot", "force_search": "exhaustive"}'
```

计划体含：`retained`、`peak_memory`、`retained_memory`、`workspace_peak`、
`forward_flops`、`recompute_flops`、`backward_flops`、`extra_compute_ratio`、
`recomputed_nodes`、`mask_only_nodes`、`replay_waves`，以及独立仿真器对账结果
`independent_check.matches_core`。

## 4. 执行训练步并核验

```bash
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/runs \
  -H 'Content-Type: application/json' \
  -d '{"master_seed": 1234, "finite_difference": true, "fd_eps": 1e-6}'
```

`verification` 段给出对独立 oracle 的损失/梯度最大绝对差、对中心有限差分的
最大绝对差、容差与 `passed`，以及未通过时的具体 `reasons`。

## 5. 应用 SGD 步

```bash
curl -s -X POST http://127.0.0.1:8000/sessions/$SID/apply \
  -H 'Content-Type: application/json' -d '{"lr": 0.1}'
```

对同一个 run 第二次 apply 返回 `409 E_STATE_RUN_DOUBLE_APPLY`。

## 6. 预算不可行（资源耗尽）

```bash
SID2=$(curl -s -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' -d '{"fixture":"tight_budget"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST http://127.0.0.1:8000/sessions/$SID2/plan \
  -H 'Content-Type: application/json' -d '{"memory_budget": 1}'
# HTTP 507
# {"error":{"code":"E_BUDGET_INFEASIBLE","category":"resource_exhausted",
#   "context":{"budget":1,"min_achievable_peak":96,"shortfall":95,...}}}
```

## 7. 提交自定义图

```bash
curl -s -X POST http://127.0.0.1:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "graph": {
      "target": "loss",
      "parameters": {"W": {"shape": [2, 2], "seed": 9}},
      "inputs": {"x": [[1.0, 0.5], [0.0, -1.0]]},
      "nodes": [
        {"id": "x", "op": "input", "params": {"shape": [2, 2]}},
        {"id": "W", "op": "parameter", "params": {"shape": [2, 2]}},
        {"id": "h", "op": "linear", "inputs": ["x", "W"]},
        {"id": "a", "op": "relu", "inputs": ["h"]},
        {"id": "loss", "op": "reduce_sum", "inputs": ["a"]}
      ]
    }
  }'
```

支持算子：`input`、`parameter`、`linear`（2 或 3 输入，第 3 个为 bias）、
`add`、`mul`、`relu`、`dropout`（`params.p ∈ [0,1)`）、`external`（带外部
副作用的恒等节点）、`reduce_sum`（输出形状 `(1,)`，作为标量损失）。
