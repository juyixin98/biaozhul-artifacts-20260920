# Bounded HTN Task Planner

有限（bounded）层次任务网络（HTN）任务规划后端：支持**方法选择**、
**顺序与部分序子任务**、**有界递归分解**、**基本动作前置条件真实验证**、
**失败分支证据**与**从抽象任务到基本动作的完整展开树**。

技术栈：Python 3.11+ · FastAPI · SQLite。全部输入为本地合成夹具，
不依赖任何生产账号或真实业务数据，可完全离线运行。

---

## 为什么不是“拼动作名”

- 基本动作只有在任务网中**极小**且其**每条前置文字在当前状态为真**时才执行；
  效果按先删后加真实推进状态，搜索在状态上回溯。
- 一个部分序任务网是 DAG（能拓扑排序）**不等于可执行**。独立验证器会枚举
  拓扑序并对每一个线性化重放状态。夹具中：
  - `docks_two`：6 个拓扑序，只有 **2** 个可执行；
  - `permit_two`：2 个拓扑序，**0** 个可执行 ⇒ 确定性 `deadlock`。
- 递归有深度/展开/动作/搜索多重预算；**预算触发结论是 `inconclusive`，
  绝不是 `failure`**。
- 无可行方法时给出 `no_applicable_method` 证据，逐个列出被拒方法与失败文字。
- 参考答案（`fixtures/expected/*.json`）为**手工编写**，独立测试据此断言
  具体动作序列、失败类别和线性化数量；独立验证器不导入规划搜索引擎。

## 模块划分（各模块承担实际工作）

| 模块 | 职责 |
|---|---|
| `htn_planner/lang.py` | 规则语言：S 表达式解析、模式校验、合取查询求基、安全否定、条件求值 |
| `htn_planner/core/engine.py` | 规划内核：有界递归分解、方法选择、全序/部分序任务网、回溯、证据 |
| `htn_planner/core/evidence.py` | 证据数据结构：动作、展开树节点、失败/不确定记录、关键步骤 |
| `htn_planner/verify.py` | **独立**验证：状态重放、层级约束、全线性化枚举、失败佐证 |
| `htn_planner/store.py` | SQLite 证据存储（runs + verifications） |
| `htn_planner/service.py` | 编排：解析 → 规划 → 独立验证 → 持久化 |
| `htn_planner/api.py` | FastAPI 查询接口（请求身份、结构化日志、失败/不确定分列） |
| `htn_planner/cli.py` | 命令行入口 `htn-plan` |
| `htn_planner/config.py` | 环境变量配置 |
| `tests/` | 独立测试（语言/内核/验证器/存储/API/CLI/夹具） |
| `fixtures/` | 可复用领域、问题与**手写**期望值 |
| `scripts/` | 验证脚本 |
| `docs/SEMANTICS.md` | 边界语义（保证什么/不保证什么/无法执行的检查） |

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 一键验证：编译 + 全部 pytest + 独立夹具核验
bash scripts/verify.sh
```

无需虚拟环境时（依赖已在系统中）也可直接：

```bash
PYTHONPATH=. python3 scripts/verify.py
```

### 命令行规划

```bash
PYTHONPATH=. python3 -m htn_planner.cli \
  --domain fixtures/domains/logistics.htn \
  --problem fixtures/problems/logistics_recursive.pddl \
  --request-id demo-1
```

输出 JSON 含 `result`（状态、动作、展开树、failures、uncertain、key_steps）
与 `verification`（独立验证结论）。规划失败也是退出码 0，结论在 JSON 中；
规则语法错误退出码为 2。

### 启动 HTTP 服务

```bash
HTN_DB_PATH=data/htn_evidence.db python3 -m uvicorn htn_planner.api:app \
  --host 127.0.0.1 --port 8000
```

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/v1/plan \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req-42' \
  -d @<(python3 -c 'import json;print(json.dumps({
    "domain":open("fixtures/domains/docks.htn").read(),
    "problem":open("fixtures/problems/docks_two.pddl").read(),
    "domain_version":"fixture-1"}))')
```

每个响应都带 `X-Request-ID` 头和 `request_id` 字段，服务端结构化日志带同一 id。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康与引擎版本 |
| POST | `/api/v1/plan` | 提交 domain/problem（可选 `bounds`、`domain_version`） |
| GET | `/api/v1/runs?status=&limit=` | 列出历史运行 |
| GET | `/api/v1/runs/{id}` | 取某次运行（含完整结果 JSON） |
| GET | `/api/v1/runs/{id}/verification` | 取该次的独立验证报告 |

`POST /api/v1/plan` 请求体：

```json
{
  "domain": "(:domain ... )",
  "problem": "(:problem ... )",
  "domain_version": "fixture-1",
  "bounds": {"max_depth": 12}
}
```

## 规则语言速览

```lisp
(:domain docks
  (:operator (!lock ?r ?w)
    (:pre (free ?r))
    (:del (free ?r))
    (:add (locked ?r ?w)))
  (:operator (!unlock ?r ?w)
    (:pre (locked ?r ?w))
    (:del (locked ?r ?w))
    (:add (free ?r)))

  ;; 方法名必填；正文字可存在性引入变量；(not ...) 须安全
  (:method m-work (work ?r ?w)
    (:pre (worker ?w))
    (:tasks (!lock ?r ?w) (!unlock ?r ?w)))

  ;; 部分序：两个子任务无序
  (:method m-parallel (parallel-work ?r ?a ?b)
    (:pre (worker ?a) (worker ?b))
    (:tasks (:partial ((work ?r ?a) (work ?r ?b)))))
)
```

## 结果三态与失败类别

- `success`：找到计划且独立验证通过。
- `failure`：搜索穷尽，确定性死路，证据在 `failures`：
  - `no_applicable_method`、`deadlock`、`cycle`。
- `inconclusive`：预算截断，未能证明可行或不可行，原因在 **独立字段**
  `uncertain`：`depth_budget` / `expansion_budget` /
  `action_budget` / `search_budget`。

`failures`（已证失败）与 `uncertain`（不确定）在响应、存储与文档中始终分列。

## 测试

```bash
python3 -m pytest tests/                                   # 全部
python3 -m pytest tests/test_engine.py -k recursion        # 按关键字
python3 -m pytest --cov=htn_planner --cov-report=term-missing
```

测试按 AAA 组织，断言具体结果与失败类别，而非“接口可调”。其中
`tests/test_verify.py` 故意篡改合法结果（交换动作、伪造动作、反转树序、
虚构死路），要求独立验证器逐一识破——证明验证不依赖被测内核自我背书。

## 语义边界与未执行检查

请务必阅读 [`docs/SEMANTICS.md`](docs/SEMANTICS.md)。它明确：

- 先删后加、方法选择、回溯、预算、循环判定的确切含义；
- 线性化枚举的有界性（上限 5000，超限置 `linearizations_truncated`）；
- 深层（非紧邻）循环不在自动确认范围内；
- **本环境不执行**需要生产账号/外部网络/超大规模负载的检查——这些在
  `scripts/verify.py` 输出与文档中单列，**不计为通过**。
