# ATMS 教学后端（基于假设集合）

一个 **Assumption-based Truth Maintenance System (ATMS, de Kleer 1986)** 的教学实现：
维护每个结论在哪些假设组合（**环境/environment**）下成立（结论的**标签/label**），
并把推出矛盾的最小环境记录为 **nogood**。技术栈：Python 3.12 · FastAPI · SQLite，
全部使用本地合成夹具，无外部账号或真实业务数据。

## 行为契约（已被测试固定）

1. **标签子集极小且一致**：任何支持环境都不被同一标签中的更小环境包含；
   nogood 本身及其所有超集都不能作为有效支持。
2. **撤销假设不删除仍有替代环境支持的结论**：`X` 由 `{A}` 和 `{B}` 独立证明时，
   撤销 `A` 后 `X` 仍由 `{B}` 支持；只有所有环境都依赖被撤销假设的结论才消失。
   撤销是**假设性场景**，不修改已存储的问题。
3. **传播终止与预算显式**：标签是有限假设集上的反链，传播按工作队列到不动点；
   步数、单标签环境数、总环境数三类预算在写入前检查。
4. **超限只报告不完整**：预算耗尽时保留已生成环境并标记 `incomplete`，
   未生成环境的节点回答 **undetermined（无法判定）**，绝不把“没算出来”当成“不成立”。

## 模块职责（非单文件脚本）

```
atms_backend/
  rules/        规则语言：language.py（DSL 解析+校验）, compiler.py（编译为内核声明）
  core/         推理内核：atms.py（标签传播、极小化、nogood 维护、hydrate 恢复）,
                budgets.py（显式预算与 BudgetExceeded）, types.py
  storage/      证据存储：schema.py（SQLite 建表）, repository.py（全部 SQL 在此）
  services/     engine_service.py：编排 解析→编译→传播→持久化；查询裁决；撤销场景
  api/          FastAPI：app.py（应用工厂）, routes.py, models.py, deps.py
  diagnostics.py  决策记录（请求标识、接受/拒绝/无法判定、原因码、脱敏）
  config.py     环境变量配置，全部有本地默认值
tests/          单元 + 集成 + 独立暴力枚举 oracle + 随机理论属性测试
fixtures/       手算样例（.atms，内含手算标签表）
scripts/        seed_demo.py（种子数据）, run.sh（一键启动）
```

## 首次运行

```bash
pip3 install --user -r requirements.txt
python3 -m scripts.seed_demo data/atms.db        # 载入样例并完成首次传播
python3 -m uvicorn atms_backend.api.app:app --host 127.0.0.1 --port 8000
# 或：bash scripts/run.sh
```

打开 http://127.0.0.1:8000/docs 可见交互式 API。

### 快速体验

```bash
# 传播后查看标签：X=[[A],[B]]，nogoods=[[A,C]]
curl -s -X POST localhost:8000/problems/shared/propagate | python3 -m json.tool

# 在含矛盾的上下文 {A,C} 中查询 X -> rejected/blocked_by_nogood，并指出 blocker
curl -s -X POST localhost:8000/problems/shared/query \
  -H 'Content-Type: application/json' \
  -d '{"node_id":"X","environment":["A","C"]}'

# 假设性撤销 A：X 仍凭 {B} 存活，nogood [A,C] 消失
curl -s -X POST localhost:8000/problems/shared/retract \
  -H 'Content-Type: application/json' -d '{"assumptions":["A"]}'
```

## 规则语言

每行一条语句，`#` 后为注释：

```
assume A, B, C            # 假设节点（自带单例环境 {A}）
fact P                    # 前提事实（由空环境 {} 支持）
rule r1: A, P => X        # Horn 式正当性：前件同时成立则后件成立
rule r5: A, C => FALSE    # 推出保留节点 FALSE 的环境即 nogood
```

只有 Horn 子句；唯一的“否定”是推出 `FALSE`。解析器带行号报错（未知节点、
保留字误用、重复 id、仅由事实推出全局矛盾等，见 `tests/test_rules_language.py`）。

## 主要 HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `POST /problems` | 创建问题（id/name/source），DSL 非法返回 422+行号 |
| `POST /problems/{id}/propagate` | 传播；body 可传预算覆盖；返回 labels/nogoods/计数器 |
| `GET  /problems/{id}/labels` | 当前完整标签与 nogoods |
| `POST /problems/{id}/query` | 查 `node_id`（可选固定 `environment`），返回决策记录 |
| `GET  /problems/{id}/nodes/{n}/explain` | 每个支持环境对应的证明规则 |
| `GET  /problems/{id}/nogoods` | 最小矛盾环境表 |
| `POST /problems/{id}/retract` | 假设性撤销，返回存活/消失/变化节点 |
| `GET  /problems/{id}/runs` | 传播运行审计（请求标识、预算计数、不完整原因） |

决策取值：`accepted`（`supported_by_environment`）、`rejected`
（`blocked_by_nogood` / `no_consistent_environment`）、`undetermined`
（`propagation_incomplete`）。

## 验证材料（参考答案不来自被测内核自身）

1. **手算标签**：`fixtures/*.atms` 顶部写明人工推导的标签与 nogood，
   测试直接断言这些具体值（多证明、共享前提、互斥假设、菱形证明、矛盾上下文）。
2. **独立枚举 oracle**：`tests/oracle.py` 用完全独立的朴素 Horn 闭包，
   枚举假设集合的**全部子集**，独立得出最小支持环境与 nogood；
   `test_oracle_crosscheck.py` 逐节点、逐子集与内核对照，并对照撤销后的标签。
3. **随机理论属性测试**：固定随机种子生成 25 个随机理论（含随机矛盾），
   在内核完成传播的前提下，所有节点标签、所有上下文下的 holds、nogood
   全部与 oracle 一致。
4. **失败类别断言**：测试断言 404/409/422/400、rejected 的具体原因码、
   undetermined 携带的预算原因，而非仅“接口可调用”。

运行测试（真实输出）：

```text
$ python3 -m pytest
........................................................                 [100%]
57 passed, 1 warning in 1.22s
```

（警告来自 starlette TestClient 对 httpx 的弃用提示，与本工程逻辑无关。）

## 诊断与脱敏

每次查询返回 `DecisionRecord`：`request_id`（可用 `X-Request-ID` 头传入）、
问题/节点、决策、原因码、查询环境、支持环境、命中的 nogood blocker、全部 nogood、
不完整标志与原因。服务端日志记录该记录；`diagnostics.redact()` 对
`token/secret/password/key/credential` 等敏感键递归脱敏（`tests/test_diagnostics.py`）。

## 配置（环境变量，均有默认值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `ATMS_DB_PATH` | `data/atms.db` | SQLite 文件路径 |
| `ATMS_HOST` / `ATMS_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `ATMS_MAX_LABEL_ENVS` | `64` | 单标签最大环境数 |
| `ATMS_MAX_TOTAL_ENVS` | `4096` | 单次传播总环境写入上限 |
| `ATMS_MAX_STEPS` | `20000` | 单次传播最大处理步数 |
| `ATMS_LOG_REDACT` | `1` | 日志脱敏开关（置 `0` 关闭） |

## 持久化说明

* 只有到达**完整不动点**的标签/nogood 才写入快照表；预算中断的部分结果
  只作为 `propagation_runs` 审计行保留，永不当作事实返回。
* 查询优先从快照 hydrate 重建引擎（重过滤 nogood、重算反链）；
  预算中断后可用更大预算从快照续跑（`ATMS.hydrate` + `propagate`，
  见 `test_resume_after_budget_eventually_completes_with_larger_budget`）。
