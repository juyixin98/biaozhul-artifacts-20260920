# 有限域 CSP 服务（合成变量网络）

面向合成变量网络的有限域约束求解服务：二元 table 关系 + all-different 全局约束，
带匹配/Hall 集传播、回溯恢复、域删减理由与 SQLite 证据存储。

## 工程分层

```
csp_service/
  model.py            规则语言：问题模式与边界校验（pydantic）
  kernel/
    matching.py       确定性二分图最大匹配（Kuhn 增广路）
    alldifferent.py   all-different 的 Régin 过滤 + Hall 证书
    propagation.py    AC-3 式传播队列引擎（含删减理由）
    solver.py         回溯搜索：快照 trail 恢复、预算、三态结果
  enumerate_ref.py    独立暴力枚举器（与内核零共享代码，仅用于验证）
  evidence.py         SQLite 证据存储（runs / events / solutions）
  api.py              FastAPI 查询接口
  config.py           配置层（CSP_* 环境变量覆盖）
  runlog.py           结构化 JSONL 运行日志（版本/步骤/判定依据）
fixtures/             可复用夹具（期望值手工推导，见各文件 expectation_basis）
scripts/verify.py     验证脚本：内核 vs 独立枚举器 + 传播健全性
tests/                独立测试层
```

## 规则语言

```json
{
  "name": "p",
  "variables": [{"name": "x", "domain": [1, 2, 3]}],
  "constraints": [
    {"type": "all_different", "id": "ad1", "vars": ["x", "y"]},
    {"type": "table", "id": "t1", "vars": ["x", "y"], "allowed": [[1, 2]]}
  ]
}
```

## 边界语义

- **值域**：仅整数；域名唯一；域非空且值互异；约束引用的变量必须存在；
  table 必须恰为二元且 allowed 对必须落在两侧域内（越界即 422/校验错误，fail fast）。
- **all-different 传播**：Régin 算法。先求变量→值最大匹配；匹配数 < 变量数即不可行，
  并输出 Hall 证书（交替路可达的变量集 S 与邻域 N(S)，|N(S)| < |S|）。
  可行时删除"不属于任何最大匹配"的边（不在匹配中、不在同一交替 SCC、
  不在自由值出发的交替路径上）。这严格强于"两两删除已赋值"：
  未赋值变量构成的 Hall 集同样触发删减（见 tests/test_alldifferent.py）。
- **二元 table 传播**：AC-3 式修订；删减理由记录对方域快照作为证据。
- **回溯恢复**：分支前将（全部域, 传播队列）快照压入 trail，子树返回后弹栈原地恢复；
  域与队列逐位回到分支前状态（tests/test_solver.py 用 53 解计数与兄弟分支用例验证）。
- **三态结果**（互斥）：
  - `SAT`：搜索完成且找到解（mode=first 找到即停；mode=all 枚举全部）；
  - `UNSAT`：搜索完成且无解（含根节点传播即失败，nodes=0）；
  - `UNKNOWN`：节点预算或传播步数预算先耗尽，`partial=true`，已找到解标记为部分结果。
    预算耗尽绝不映射为成功或无解。
- **预算**：`max_nodes`（分支节点数）、`max_propagation_steps`（约束修订次数）、
  `max_solutions`；均可经 API 或 `CSP_*` 环境变量配置。
- **证据**：每次求解落库一个 run（问题、配置、工具版本、状态、统计）+ 有序事件流
  （prune 含机器可读理由、wipeout、hall_violation、branch、backtrack、
  budget_exceeded、solution、status）。被回溯放弃分支的事件保留，
  以 node/depth 命名空间区分，搜索树可完整审计。
- **确定性**：变量按名称、值按升序访问，匹配与删减理由可复现。
- **规模边界**：搜索为递归实现，变量数受 Python 递归限制约束（约 900）；
  匹配为 O(V·E)，面向中小规模合成网络。

## 运行

```bash
pip install -r requirements.txt          # 版本已固定
python -m pytest                          # 31 个测试
python scripts/verify.py                  # 夹具验证，退出码即判定
uvicorn csp_service.api:app --port 8000   # 服务
```

API：`GET /health`、`POST /problems`、`GET /problems/{name}`、
`POST /solve`（内联 problem 或 problem_name；mode/budgets）、
`GET /runs/{run_id}`、`GET /runs/{run_id}/events?kind=prune`、
`GET /runs/{run_id}/prunings`。错误映射：校验失败 422、缺参 400、
未知问题/运行 404；预算耗尽返回 200 + `UNKNOWN`（不是错误，也不是成功）。

## 验证方案

- **夹具**（fixtures/，期望值手工推导并写明 `expectation_basis`）：
  - `hall_conflict`：3 变量 2 值，根节点 Hall 冲突，nodes=0，UNSAT；
  - `isolated_variable`：弧一致删减 x=3/y=1，孤立变量 z 域保持 {1,2,3,4}，8 解；
  - `multi_solution`：K₃,₃ 根传播零删减，3! = 6 解；
  - `deep_backtrack`：5 变量无相邻连续排列，D₅+D₄ = 53 解，强制深层回溯。
- **对照**：`enumerate_ref.py` 暴力枚举与内核零共享代码；verify.py 断言
  解集相等、计数等于手写期望、且**每个搜索节点上每个被删 (var,value)
  不出现在任何与该节点部分赋值一致的参考解中**（传播不删真实解取值）。
- **日志**：`logs/<run_id>.jsonl`，每条含 run_id、步骤号、版本集合、
  输入指纹（问题 SHA-256）、判定依据（basis）与 verdict；异常/未知状态
  不会记为 pass。

## 无法执行或未执行的检查（单列，不计为通过）

- 未做并发写压测：SQLite 单连接 + 锁已序列化，但未模拟多进程同时写库。
- 未做大规模性能验证：>100 变量或域 >10⁴ 的实例未测试。
- 未做多 worker uvicorn 部署验证（内存态 problem 注册表不跨进程共享）。
- 未做模糊化（fuzz）输入测试；边界校验仅由定点测试覆盖。
- 递归深度边界（约 900 变量）未实测到上限。
