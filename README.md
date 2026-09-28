# 小型加权有限状态转换器（WFST）服务

一个基于 **Python 3.12 + FastAPI + SQLite** 的小型加权有限状态转换器服务，支持：

- **词典映射**：`input → [(output, cost), ...]` 在装载期展开为字符级转换器（trie 消费 + 逐字符发射链）；
- **ε-过滤组合**：两台转换器按中间磁带真实符号同步，单侧 ε 移动由三态过滤器按规范序 `B* A*` 调度，**避免同一对齐被重复计数**；
- **最短输出枚举**：固定输入下，按 **(总代价升序, 输出字典序升序)** 返回去重后的 k 个输出，使用 Bellman-Ford 反向势能 + 虚拟目标节点的一致启发式 A*；
- **预算耗尽显式未完成**：扩展预算用尽时抛出 `budget_exhausted`（HTTP 503），绝不把截断结果当成功返回。

全部数据均为**本地合成夹具**，无生产账号、无真实业务数据、无网络依赖。

---

## 1. 目录结构（分层工程）

```
wfst_service/
├── corpus/                  # 语料规范层
│   ├── symbols.py           #   符号规范：单字符标签、<eps> 归一化
│   ├── errors.py            #   结构化错误码（spec/cycle/budget/not_found/query）
│   ├── spec.py              #   JSON 模式手工校验 + fst/lexicon 构建
│   └── serialize.py         #   FST ↔ JSON（供索引持久化）
├── core/                    # 挖掘内核（与 HTTP/SQLite 完全解耦）
│   ├── fst.py               #   不可变 Arc/Fst 模型 + 恒等 acceptor
│   ├── eps_removal.py       #   纯 (ε,ε) 静默弧消除（Floyd-Warshall 热带闭包）
│   ├── compose.py           #   三态 ε-过滤组合（含审计计数）
│   ├── cycles.py            #   ε 环（三色 DFS）+ 负代价环（Bellman-Ford）检测
│   └── search.py            #   势能 + 虚拟目标 A* 的 k-最短输出枚举
├── index/                   # 索引与模型层（SQLite）
│   ├── db.py                #   连接/DDL
│   ├── repository.py        #   语料/模型/运行/结果/日志仓库
│   └── service.py           #   装载、环校验、流水线预组合、物化
├── api/                     # 查询验证 / 传输层
│   ├── schemas.py           #   pydantic 请求/响应模型
│   ├── service.py           #   查询校验边界 + 编排 + 错误分类
│   └── app.py               #   FastAPI 路由
├── config.py                # 独立配置层（环境变量）
├── logging_setup.py         # 关联 run_id 的 JSON 行日志
└── main.py                  # uvicorn 入口

fixtures/corpora/            # 最小合成夹具（1 个正常 + 2 个负例）
scripts/                     # 装载脚本 + 服务调用示例
tests/
├── oracle.py                # 独立暴力预言机（不导入任何被测组合/搜索代码）
├── test_symbols.py
├── test_spec_validation.py
├── test_fst_model.py
├── test_cycles.py
├── test_compose.py
├── test_search.py
├── test_oracle_crosscheck.py## 短输入穷举路径对照 + 组合 vs 顺序执行
├── test_index_integration.py
├── test_query_service.py
└── test_e2e_api.py          # 真实 FastAPI（TestClient）端到端
examples/results/            # 已保留的可复核真实运行结果
```

这不是单文件实现，也不是仅有调用壳或固定返回值：每个层都有独立职责与独立测试。

---

## 2. 语义与支持范围（明确界定）

### 2.1 输入 ε 与输出 ε 分离

内部都用空串 `""` 表示 ε（夹具中写作 `"<eps>"`），但二者是**不同磁带位置**，组合时**永不互相同步**：

| 弧标签 | 含义 |
|---|---|
| `(x, y)` | 消费真实符号 `x`，发射真实符号 `y`（同步） |
| `(x, ε)` | 删除：消费 `x`，不发射（A 单侧移动） |
| `(ε, y)` | 插入：不消费，发射 `y`（B 单侧移动） |
| `(ε, ε)` | 纯静默弧（装载期先消除，便于无歧义调度） |

### 2.2 环的支持范围

- **纯 ε 环**（环上每条弧都是 `(ε,ε)`，消费/发射均为空）→ **装载期拒绝**，返回证据状态序列。它们会产生无穷多个完全相同的对齐；
- **负代价环**（任何从起点**可达**的负环；保守地也包括到不了终态的环）→ **装载期拒绝**（Bellman-Ford 检测，终态权值经虚拟汇点计入），否则最短代价无下界；
- **正代价真实符号环 / 正代价 ε-插入环**：允许（关系可无限），枚举由 `k` 与扩展预算界定；
- 无环的负权弧合法（折扣路径），势能仍为有限值。

### 2.3 组合状态生成与去重

组合状态为双侧进度三元组 `(state_A, state_B, filter_phase)`。同步只在**真实中间符号**且 B 弧源状态等于当前 `state_B` 时发生；单侧 ε 移动在相位
`FREE / B-OPEN / A-OPEN` 之间按规范序 `B* A*` 转移。当 A、B 两侧 ε 移动同时可触发时，朴素调度会把同一对移动按两种顺序各计一次；过滤器抑制非规范
交织，**只删除完全相同的副本，不删除任何不同对齐或可达端点**。

### 2.4 排序与完备性

- 多个输出按 **(总代价, 字典序)** 返回，同一输出的多条对齐在 `(GOAL, output)` 配置上合并为最小代价；
- 同代价平局类会在阈值上整体排空后再做字典序裁决，保证 k 截断对平局公平；
- 预算耗尽 = **未完成**（`complete=false`，HTTP 503，`error_code=budget_exhausted`）；无路径是确定的空答案（`status=no_path`，非异常）。

---

## 3. 快速复现

需要 Python 3.12（3.10+ 亦可）。无外网时直接使用环境中已装的包；需要全新环境时：

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.lock   # 锁定版本，可复现
```

### 3.1 运行测试（约 4 秒）

```bash
python3 -m pytest tests/ -v
# 带覆盖率（已验证 95% 总体覆盖率，超过 80% 门槛）：
python3 -m pytest tests/ --cov=wfst_service --cov-report=term-missing
```

最近一次完整结果保存在 `examples/results/pytest_report.txt`：**77 passed**。

### 3.2 真实服务运行

```bash
# 终端 1：启动（默认 data/wfst.sqlite3，可用环境变量覆盖，见 §6）
python3 -m wfst_service.main

# 终端 2：经 HTTP 装载合成夹具（含两个应被拒绝的负例）
python3 scripts/load_fixtures.py --base-url http://127.0.0.1:8000

# 终端 2：运行全部调用示例（正常 + 无路径 + 404 + 503）
python3 scripts/call_examples.py
```

不经服务、直接写库装载：

```bash
python3 scripts/load_fixtures.py --db data/wfst.sqlite3
```

已保留的真实运行产物：

- `examples/results/load_fixtures.log`：正常装载 200，ε 环/负环夹具均 422 `cycle_error`；
- `examples/results/call_examples.log`：六个场景的完整 JSON 响应；
- `examples/results/server_json_logs.log`：关联 run_id / 输入 / 版本的 JSON 日志；
- `examples/results/pytest_report.txt`：带覆盖率的详细测试报告。

---

## 4. 验证方法说明（参考答案独立性）

`tests/oracle.py` 是**独立暴力预言机**：直接在 `(src,dst,in,out,cost)` 元组上做 DFS 穷举，不导入
`wfst_service.core.compose` / `wfst_service.core.search` 的任何代码，因此参考答案不可能由被测核心自己生成。

针对 `kat / ca / dog / katz / cats` 等短输入（长度 ≤ 4）做**全路径穷举**对照：

1. **单转换器对照**：预言机穷举字符纠正机的全部 `输出→最小代价`，与 SUT k-best 逐项断言；
2. **组合对照**：预言机独立枚举左机关系，再对每个中间串独立枚举右机关系（关系组合），与预组合流水线结果逐项断言；
3. **组合 vs 顺序执行**：先用左级枚举中间串（带可证明充分的代价阈值），逐串查询右级后求和取 min，断言与预组合结果、与预言机三方一致；
4. **截断可证明**：合成夹具含正代价 ε-插入自环（关系无限）。预言机限制 `(state,pos)` 重访次数并记录截断前沿代价；由于先断言所有弧权非负，
   被截断的后代代价只增不减。测试进一步断言**截断前沿严格高于第 k 名代价**（实测前沿 9.0 ≫ 第 8 名 ≤ 3.5），从而证明截断不影响 top-k 对照。
   若夹具规模使硬步数上限触顶，预言机直接 `AssertionError`，不会给出被悄悄截断的“参考答案”。

测试断言的是**具体结果与失败类别**（具体输出串、具体代价、具体 HTTP 状态码与 `error_code`），不是“接口能调用”。

---

## 5. 失败类别（不把异常/未知状态统一返回成功）

| 场景 | error_code | HTTP | complete |
|---|---|---|---|
| 正常命中 | — | 200 `status=ok` | true |
| 输入无接受路径 | — | 200 `status=no_path` | true（空答案确定） |
| 预算耗尽、未完成 | `budget_exhausted` | **503** | **false** |
| 未知语料/目标 | `not_found` | 404 | false |
| 请求非法（长度/k/budget/ε 字面量等） | `query_error` | 422 | false |
| 语料 JSON 模式错误（带 JSON 路径） | `spec_error` | 422 | — |
| ε 环 / 负代价环（带证据状态） | `cycle_error` | 422 | — |

每次查询（含失败）都写 `runs` / `run_logs` 表，可用
`GET /corpora/{corpus_id}/runs/{run_id}` 复核其输入、状态、结果与完整计算轨迹；控制台日志为带 `run_id / corpus_id / target / input / version` 的 JSON 行。

日志中可看到判定依据，例如：

```
search run=run-... fst='pipeline:correct_then_morph' input='kat' k=3 budget=50000
verdict: complete:k_reached(3)
compose ... states=11 arcs=22 moves: sync=8 a-alone=0 b-alone=32
backward potentials (min remaining cost to virtual goal): 0:0, 1:0, ...
settle goal output='kat' total_cost=0 [#1 distinct]
settle goal output='kats' total_cost=0.3 [#2 distinct]
decision basis: virtual-goal A* with consistent potentials ...
```

---

## 6. 配置（环境变量）

| 变量 | 默认值 | 含义 |
|---|---|---|
| `WFST_DB_PATH` | `data/wfst.sqlite3` | SQLite 路径（测试用 `:memory:`） |
| `WFST_DEFAULT_K` / `WFST_MAX_K` | 5 / 100 | 默认与最大输出数 |
| `WFST_DEFAULT_BUDGET` / `WFST_MAX_BUDGET` | 100000 / 5000000 | 默认与最大扩展预算 |
| `WFST_MAX_INPUT_LENGTH` | 128 | 最大输入长度 |
| `WFST_HOST` / `WFST_PORT` | 127.0.0.1 / 8000 | 监听地址 |
| `WFST_LOG_LEVEL` | INFO | 日志级别 |

---

## 7. API 摘要

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查（含版本、DB 路径） |
| `GET /version` | 服务版本与语料 schema 版本 |
| `POST /corpora/{id}/load` | 内联 JSON 装载语料（校验+环检测+预组合+物化） |
| `GET /corpora` | 列出语料、转换器与流水线 |
| `POST /query` | `{corpus_id,target,input,k,budget}` → 排序输出 + 轨迹 |
| `GET /corpora/{id}/runs/{run_id}` | 复核持久化运行、结果与日志 |

语料 JSON 形状见 `fixtures/corpora/demo_corpus.json`（`version: 1`，`kind` 为 `fst` 或 `lexicon`，
`pipelines[].sequence` 至少 2 个已命名转换器）。
