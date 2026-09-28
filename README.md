# 小型加权有限状态转换器（WFST）服务

一个用 **Python + FastAPI + SQLite** 实现的加权有限状态转换器服务，支持：

- **词典映射**：整串输入 → 一个或多个整串输出（同输入多输出歧义）；
- **组合（composition）**：字符纠错级联 ∘ 词形规则 ∘ 词典，带 epsilon 顺序过滤器；
- **最短输出（n-best）**：热带半环下按总代价升序、平手按稳定字典序返回多个输出；
- **预算与完成标志**：预算耗尽时返回已找到结果并显式标记 `complete=false`（未完成），
  绝不伪装成成功；负代价环显式报错。

所有数据均为**本地合成夹具**（`fixtures/corpora/*.json`），无生产账号、无真实业务数据。

---

## 1. 目录结构（按可维护工程分层）

```
src/wfst/
  core/        FST 数据结构、输入/输出 epsilon 语义、负环与 epsilon 环检测
  algorithms/  epsilon 三态顺序过滤器、组合、n-最短路径（Johnson 势函数 + best-first）
  corpus/      语料规范（pydantic 校验）、挖掘内核（独立 Levenshtein 对齐 + 平滑代价）
  index/       SQLite 仓储、把挖掘结果编译成 FST 并预组合查询管线
  service/     FastAPI 应用、结构化运行日志（run_id 关联）
  query.py     查询边界校验、失败类别、一次性组合 & 分阶段顺序执行两条计算路径
  builders.py  词典/编辑级联/规则/链接受机的 FST 工厂
  config.py    环境变量配置（启动即校验）
fixtures/corpora/  三个合成语料夹具（字符纠错+词形 / epsilon插删+歧义 / 词级）
tests/             独立测试（81 个），含与被测核心解耦的穷举 oracle（tests/oracle.py）
scripts/           端到端复现、真实 HTTP 调用、依赖闭包锁定脚本
results/           已归档的真实运行结果（正常 + 异常）
```

这不是单文件实现，也不是只有调用壳或固定返回值：每一层都有独立职责与独立测试。

---

## 2. 关键语义与“支持范围”约定（明确，不含糊）

### 2.1 输入 epsilon 与输出 epsilon 分离

同一个字面量 `<eps>`，靠它在弧上的**位置**区分：

| 弧 | 含义 | 对输入的影响 |
|----|------|--------------|
| `x : <eps>` | **删除**（输出 epsilon） | 消耗一个输入 `x`，不产出 |
| `<eps> : y` | **插入**（输入 epsilon） | 不消耗输入，产出 `y` |
| `<eps> : <eps>` | 纯 epsilon 跳转 | 两侧都不推进 |

`Arc.kind()` 把弧分为 `NORMAL / EPS_IN / EPS_OUT / EPS_EPS` 四类。

### 2.2 组合的 epsilon 过滤（避免重复路径计数）

组合 `M1 ∘ M2` 时，M1 的输出 epsilon 走法（L）与 M2 的输入 epsilon 走法（R）
互不干涉、可交换。朴素展开会让同一条对齐以不同交错顺序被**重复计数**。

本实现在“真实符号匹配分隔的每个 epsilon 块”内强制规范序 **`L* R*`**
（过滤器三态 `NEUTRAL / LEFT_RUN / RIGHT_RUN`）：

- 完备：L 只改 q1、R 只改 q2，任何交错都能重排成 L*R*（如“删除 a 再插入 b”
  的 `a → b` 合法，过滤器在 L 后放行 R）；
- 唯一：R 之后阻塞 L，故每条接受对齐恰有一条路径。

### 2.3 epsilon 环与负代价环

- **零/正代价 epsilon 自环**：允许。n-best 用 `(状态, 输出串)` 优势去重，
  best-first 下不会死循环。
- **纯输入 epsilon 发射环**（不吃输入却不断产出）：可产生无穷多输出，
  由**预算**终止，结果带 `complete=false`，调用方据此知道只是前缀。
  `core.cycles.reachable_input_epsilon_cycle` 可显式报告此类环。
- **负权弧（非环）**：支持。先用 Bellman-Ford 算到虚拟汇点的最短距离作为
  Johnson 势函数，把边权重标度为非负再做 best-first，报告的仍是**原始代价**。
- **负代价环**：若位于“初态可达且能到终态”的接受路径上，最短路径无定义，
  抛出 `NegativeCycleError` 并携带环上状态，**绝不返回看似成功的结果**。

### 2.4 多个输出的排序

主键总代价升序；代价相同按输出串的 Python Unicode 码位序（稳定字典序）。
同一输出串若有多条对齐，只保留最小代价。

---

## 3. 快速开始

需要 Python 3.10+（开发与验证在 3.12 上完成）。

```bash
# 可选：建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 安装依赖（可复现请用锁定文件）
python3 -m pip install -r requirements-lock.txt
# 或只装直接依赖： python3 -m pip install -r requirements.txt
```

### 3.1 跑测试（独立测试层）

```bash
python3 -m pytest                       # 81 个测试
python3 -m pytest --cov=src/wfst        # 覆盖率（实测整体约 95%，每个源文件 >80%）
```

### 3.2 端到端复现（正常 + 异常，结果落盘可复核）

```bash
PYTHONPATH=src python3 scripts/run_demo.py
```

产物：

- `results/demo_results.txt`：人类可读摘要；
- `results/demo_results.json`：按 `run_id` 关联的完整结果（含计算步骤 `steps`）。

### 3.3 真实 HTTP 服务调用

```bash
# 终端 A：启动服务（启动时自动摄取 fixtures/corpora 下的语料）
PYTHONPATH=src WFST_DB_PATH=./data/wfst.db \
  python3 -m uvicorn wfst.service.app:app --host 127.0.0.1 --port 8000

# 终端 B：让脚本后台起服务并用 httpx 打一遍正常/异常请求
PYTHONPATH=src python3 scripts/serve_example.py
# 结果归档到 results/service_calls.jsonl
```

也可用 curl：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d '{"corpus_id":"char_morph_demo","input":"kat","k":5}'
curl -s -X POST localhost:8000/query/cross-check -H 'Content-Type: application/json' \
  -d '{"corpus_id":"char_morph_demo","input":"citi","k":6}'
```

---

## 4. HTTP 接口

统一响应信封：`{"success": bool, "data": ..., "error": ...}`。

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查，含 `service_version` |
| `GET /models` | 已加载模型列表（语料/版本/管线规模） |
| `POST /models/ingest` | 摄取本地语料 JSON：`{"path": "..."}` |
| `POST /query` | n-最短转换 |
| `POST /query/cross-check` | 一次性组合 vs 分阶段顺序执行，逐条比对 |

`POST /query` 请求体：

```json
{ "corpus_id": "char_morph_demo", "version": "1.0.0",
  "input": "kat", "k": 5, "budget": 200000,
  "mode": "composed" }
```

`mode`：`composed`（一次性组合）或 `stagewise`（分阶段顺序执行）。
需要两路同时比对请用 `POST /query/cross-check`。

失败类别（不统一返回成功）：

| category | HTTP | 含义 |
|---|---|---|
| `model_not_found` | 404 | 模型未加载 |
| `empty_input` | 422 | 输入为空 |
| `input_too_long` | 422 | 超长 |
| `unknown_symbol` | 422 | 含字母表外符号（`details.unknown` 列出） |
| `invalid_parameter` | 422 | k/预算非法 |
| `unacceptable_input` | 422 | 符号已知但无任何接受路径 |
| `negative_cycle` | 422 | 接受路径上存在负代价环 |
| `corpus_validation` | 422 | 语料文件不符合规范（`errors` 列出**全部**失败项） |

预算截断是**非致命未完成**：HTTP 200、`success=true`，但 `data.complete=false`、
`data.status="incomplete"`。交叉核验不一致时返回 **409** 且 `success=false`。

---

## 5. 语料夹具规范（最小数据夹具）

版本化 JSON：

```json
{
  "corpus_id": "char_morph_demo",
  "version": "1.0.0",
  "token_level": "char",
  "edit_tags": ["spell"],
  "alignments": [
    {"input": "kat", "output": "cat", "count": 12, "tag": "spell"},
    {"input": "cat", "output": "cats", "count": 18, "tag": "lex"}
  ],
  "rules": [
    {"ilabel": "y", "olabel": "i", "weight_hint": 0.35, "kind": "sub"}
  ]
}
```

- `tag` 在 `edit_tags` 内的对齐（如 `spell`）用于挖掘**编辑代价**；
  其余（如 `lex`）构成**词典映射**，二者隔离，避免错拼形式被零代价接受。
- 编辑代价由一个**独立实现的 Levenshtein DP** 回溯出字符对齐，按操作类型分组做
  加 α 平滑的多项分布：`cost = -ln((c+α)/(Σc + α·K))`。
- `rules` 是显式词形规则，编译成与“挖掘编辑级”分离的第二级。
- `token_level` 为 `word` 时按空格切词，并在内部给每个词加边界后缀以无歧义对齐。

三个夹具：

1. `char_morph_demo.json`：字符纠错（k→c、z→s、插删）+ 词形规则（y→i）+ 歧义；
2. `epsilon_ambiguity_demo.json`：聚焦 ε 插入/删除与多输出，规模刻意小以便穷举；
3. `word_morph_demo.json`：词级（go/walk 的词形）。

---

## 6. 测试如何“断言具体结果”，而非只测接口可调

- **具体输出与代价**：如 `kat → cat(4.117878), cats(4.901409)`，
  代价由人工按平滑公式手算后在测试里断言（`tests/test_mining.py`）。
- **失败类别**：空输入、字母表外符号、不可接受输入、负代价环、非法参数、
  预算未完成，分别断言具体 category 与 HTTP 状态码。
- **短输入穷举路径对照**：`tests/test_crosscheck.py` 对每个短输入比较三方结果：
  1. 被测核心 `run_query`（一次性组合 + n-best）；
  2. `transduce_stagewise`（分阶段顺序执行，不同计算路径）；
  3. **独立穷举 oracle** `tests/oracle.py`：不导入 `compose/nbest`，
     直接对三级 FST 做带插入界的 DFS 字符串解释，min-plus 合并；
     并用 4 / 8 两个插入界断言结果与界无关。

  因此参考答案**不是**由被测核心自身生成。

### 运行日志可关联、可复核

每次查询/摄取/异常都写一行 JSON 到 `results/runs.jsonl`，字段含
`run_id`、`ts`、`service_version`、`corpus`（含版本）、`input`、
计算步骤 `steps`、候选代价与最终 `verdict`。异常记为
`verdict="error:<category>"`，从不出现在成功汇总里。

---

## 7. 已归档的真实运行结果

- `results/demo_results.txt` / `.json`：端到端正反用例，总体判定 `ALL_OK`；
- `results/service_calls.jsonl`：真实 uvicorn 服务的 HTTP 调用（200/404/422/未完成）；
- `results/runs.jsonl` / `runs.log`：结构化与文本运行日志。

> 这些文件是在本机真实执行后保留的，可删除后用第 3 节命令重新生成复核。

---

## 8. 复现实验里几条可核验的预期

- 组合与顺序执行对全部夹具短输入逐条一致（`agree=true`）。
- 输入 `ab`（epsilon 夹具）按代价得到 `B, AB, ab` 三个不同输出。
- 极小预算（如 `budget=10`）对含插入自环的模型返回 `complete=false`。
- 构造的负代价 ε:ε 自环触发 `NegativeCycleError`；零代价 ε:ε 自环正常终止。
- SQLite 落库后，用全新内存管理器 `load_from_store` 重建模型，结果与首次一致。
