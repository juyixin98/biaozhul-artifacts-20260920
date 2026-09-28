# 带类型括号结构索引（Typed Bracket Structure Index）

对大文本建立**带类型**括号结构索引，支持：

- 局部插入/删除编辑，只失效受影响路径，不重扫无关全文；
- 匹配跳转（给定括号偏移，返回配对括号位置）；
- 最短不平衡区间（错配 / 多余闭括号 / 未闭合开括号）；
- 基于**偏移版本**的乐观并发：旧版本编辑被拒绝，防止错位；
- 索引分块流水线与完整栈扫描的在线一致性校验。

技术栈：Python 3.10+（开发/验证环境为 3.12）、FastAPI、SQLite（标准库 `sqlite3`）。
所有数据与依赖均为本地合成夹具，无需任何生产账号或外部服务。

---

## 1. 模块关系（每个模块有真实职责）

```
app/
├── corpus.py        语料/词法规范：括号类型、引号与转义声明、行/块注释；
│                    另含与内核无关的确定性合成文档生成器（自制 LCG）。
├── mining/          挖掘内核
│   ├── lexer.py     唯一决定“哪些字符是结构”的地方：引号/转义/注释掩码，
│   │                支持跨块的开放掩码状态（OpenMask）续扫。
│   ├── chunker.py   掩码安全分块：边界绝不落在引号/转义/注释内部（二分定位）。
│   ├── tokens.py    块归约（前缀未配闭括号序列、后缀未配开括号序列、匹配/错配
│   │                事件）及其组合单子 combine；保留类型“次序”而非净计数。
│   ├── scanner.py   完整栈扫描（产品侧参考路径）+ 缺陷分类与最短区间。
│   └── index.py     Treap/rope 块索引：split/merge 外科手术式局部编辑，
│                    仅沿受影响根路径 pull 重算；行级 SQLite 持久化。
├── storage.py       SQLite 连接、schema、文档仓库、Reduction JSON 编解码。
├── service.py       门面：版本检查、编辑失效统计、查询、与完整扫描比对。
├── validation.py    接受 / 拒绝 / 无法判定（accept/reject/undetermined）闸门。
├── diagnostics.py   带 request_id 与关键状态的诊断记录；上下文片段脱敏。
├── models.py        Pydantic 请求/响应模型（请求与响应类型分离）。
├── dependencies.py  应用状态、请求 ID 注入、写端点固定窗口限流。
├── api.py           瘦路由：统一响应信封与错误映射。
├── main.py          create_app() 工厂与启动入口。
├── seed.py          本地演示数据播种 CLI。
└── config.py        环境变量配置（无第三方 settings 依赖）。

tests/
├── fixtures/
│   ├── oracle.py    独立参考预言机：从零手写的字符级状态机，不 import 任何
│   │                被测内核代码（参考答案不由被测核心自己生成）。
│   └── cases.py     手工用例：每个用例钉死具体偏移与失败类别。
├── test_lexer.py    词法边界（引号/转义/注释/跨块掩码）。
├── test_monoid.py   “计数相等但交叉错配”、单子结合律与单位元。
├── test_chunker.py  分块边界不得切断字符串/注释。
├── test_kernel.py   手工用例 × 多种块大小、合成文档对独立预言机、深嵌套。
├── test_edits.py    局部插删、失效范围（行 id/偏移不变量）、版本冲突、
│                    跨多块引号、40 步随机编辑序列、持久化往返。
├── test_api.py      HTTP 端到端：断言具体结果、失败类别、脱敏、请求 ID。
└── test_api_extra.py 剩余边界与拒绝分支。
```

依赖方向是单向的：`api → validation → service → mining/index → {chunker, tokens, lexer}`，
`corpus` 是被大家引用的规范，`storage` 只被 `index/service` 使用。

---

## 2. 算法假设（请在评审前先读这一节）

1. **词法优先级**：引号与注释内的括号一律不是结构。引号是否闭合、转义如何
   生效，完全由 `corpus.BracketLexicon` 的声明决定，扫描器不做猜测。
   默认规范：`()`/`[]`/`{}` 三类；`"` 双引号，`\` 转义（`\"`、`\\`）；
   `//` 行注释、`/* */` 块注释。
2. **转义规则**：转义符精确吞掉后一个字符（包括越过块尾）；因此
   `"a\"b"` 中内部引号不闭合，`"\\"` 中引号在第 4 列闭合。未闭合引号
   持续掩码到文末，并在诊断中标记 `terminated=false`。
3. **错配约定**（产品内核与完整栈扫描、独立预言机三方一致）：
   - 闭括号遇空栈 → `STRAY_CLOSE`（多余闭括号，区间为单点）；
   - 闭括号与栈顶**同类型** → 匹配，弹栈；
   - 闭括号与栈顶**异类型** → `TYPE_MISMATCH`，开括号与该闭括号**都丢弃**
     （编辑器 bracket-jump 的通行约定：二者不配对，也不再参与后续配对）；
   - 扫描结束仍在栈中 → `STRAY_OPEN`，区间从该开括号延伸到文末。
4. **最短不平衡区间**：在所有缺陷区间中取长度最小者（错配区间天然很紧，
   未闭合区间到 EOF 通常最长）；长度相同取起点最小。
5. **“计数相等”不是平衡**：`([)]` 每种类型净计数都是 0，但有两处交叉错配。
   块摘要保留前缀/后缀的**带类型有序序列**，组合时只在接缝处重放栈机，
   因此结论与完整栈扫描严格一致（`combine` 满足结合律，空归约为单位元）。
6. **分块边界掩码安全**：对待分块文本做一次词法扫描，边界选在不被任何
   引号/注释区间覆盖的位置；宁可让块超过目标大小，也不切断掩码。
7. **编辑局部性**：编辑区间先扩张到整块；用 treap split 切出中段删除，
   仅对该窗口重词法、重分块，再 merge，pull 只发生在受影响根路径。
   若重写后的窗口结束在未闭合引号/块注释中，则向右吞并后继块直到掩码
   闭合（内部块边界原始状态必然是“干净”的，因此只需向右传播）。
8. **偏移版本**：文档创建为 version 1，每次成功编辑 +1。编辑必须携带
   `base_version`，与当前版本不符直接拒绝（HTTP 409，`VERSION_CONFLICT`），
   不触碰任何块，避免旧偏移错位。
9. 复杂度：构建 O(n)；编辑 O(受影响窗口 + log 块数·树高路径 pull)；
   `match`/`defects`/`verify` 读取根摘要（事件已含全局坐标），当前实现的
   结构合法性判断伴随一次全文词法扫描 O(n)，对本演示规模足够。
10. 并发：单进程单 SQLite 连接（WAL），适合本地演示；未做多进程写协调。

---

## 3. 本地验证命令与预期判断方式

### 3.1 安装

```bash
cd /home/admin/Downloads/xinbiaozhul/opp437/a
python3 -m venv .venv && . .venv/bin/activate        # 可选
pip install -r requirements.txt
```

### 3.2 运行全部测试（主验收命令）

```bash
python3 -m pytest tests/ -q
```
**预期：全部通过**（开发机实测 `126 passed`），无 failed/error。

带覆盖率（门槛 80%）：
```bash
python3 -m coverage run -m pytest tests/ -q
python3 -m coverage report
```
**预期：TOTAL 覆盖率 ≥ 90%**（开发机实测 95%），每个 `app/` 模块均 ≥ 79%，
核心内核 `mining/*` 均 ≥ 94%。

### 3.3 针对性验收点（题目点名的四类）

| 验收点 | 对应测试 | 断言方式 |
|---|---|---|
| 计数相等但交叉错配 | `test_monoid.py::test_crossing_types_equal_counts_but_not_balanced`、`test_api.py::test_crossing_types_report_mismatch_and_shortest_interval` | 净计数为 0 但 `balanced=false`，两处错配偏移 `(1,2)/(0,3)`，最短区间 `[1,3)` |
| 多块嵌套 | `test_kernel.py::test_multi_chunk_nesting_depth`、`..._agrees_with_full_scan`（块大小 1/2/3/5/16/64 参数化） | 深 50 层在块大小 7 下仍 50 个匹配；分块结果恒等于完整扫描 |
| 转义 | `test_lexer.py` 的 `escaped_*`、`test_kernel.py[escaped_backslash_*]` | `\"` 不闭合、`\\` 后引号闭合，具体 token 偏移 |
| 局部插删 | `test_edits.py` 全部，尤其 `test_edit_only_invalidates_affected_blocks`、`test_random_edit_sequence_always_agrees_with_oracle` | 窗口外块**行 id 不变**、窗后块文本不变仅整体平移净长度；40 步随机编辑步步对照独立预言机 |

单独跑某个用例：
```bash
python3 -m pytest "tests/test_kernel.py::test_indexed_pipeline_agrees_with_full_scan[counts_equal_but_crossing-1]" -q
```

### 3.4 启动服务并手工冒烟

```bash
BRACKET_INDEX_DB=/tmp/demo.db BRACKET_INDEX_CHUNK_SIZE=16 \
  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
# 创建：([)] 后面跟一个引号串，引号里的 ( 必须被掩码
curl -s -X POST localhost:8000/documents -H 'Content-Type: application/json' \
  -d '{"name":"demo","content":"([)] \"a(b\""}'
# 预期 data.length=10，version=1

# 最短不平衡区间：预期 shortest.category=TYPE_MISMATCH, interval=[1,3)
curl -s "localhost:8000/documents/1/defects"

# 匹配跳转：偏移 0 的 '(' 在此例中是错配 → matched=false 并带 defect
curl -s "localhost:8000/documents/1/match?offset=1"

# 在线一致性：索引分块流水线 == 完整栈扫描，agrees=true
curl -s "localhost:8000/documents/1/verify"

# 旧版本编辑：预期 HTTP 409，reason=VERSION_CONFLICT，state 带 current_version
curl -s -X POST localhost:8000/documents/1/edits -H 'Content-Type: application/json' \
  -d '{"start":0,"end":0,"replacement":"[","base_version":99}'

# 诊断记录（带 request_id、脱敏状态）
curl -s "localhost:8000/diagnostics?limit=20"
```

### 3.5 合成数据播种 CLI

```bash
BRACKET_INDEX_DB=/tmp/seed.db python3 -m app.seed
```
预期：`synthetic_balanced` 约 4k 字符、`verify agrees=True balanced=True`；
`handcrafted_defects` 输出一个具体 `TYPE_MISMATCH` 区间。

### 3.6 无状态文本分析（不落库）

```bash
curl -s -X POST localhost:8000/query/analyze -H 'Content-Type: application/json' \
  -d '{"content":"([)]","offset":null}'
# balanced=false，defects 两条 TYPE_MISMATCH；正文以未闭合引号结尾时
# HTTP 体 status=undetermined（仍给出答案，但说明无法完全判定）。
```

---

## 4. HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查与生效块大小 |
| POST | `/documents` | 创建文档并构建索引 |
| GET  | `/documents` / `/documents/{id}` | 列表 / 元数据（含版本、长度） |
| POST | `/documents/{id}/edits` | 局部编辑（需 `base_version`），返回失效窗口与重扫字符数 |
| GET  | `/documents/{id}/match?offset=` | 匹配跳转 |
| GET  | `/documents/{id}/defects` | 全部缺陷 + 最短不平衡区间 |
| GET  | `/documents/{id}/verify` | 索引 vs 完整栈扫描一致性 |
| POST | `/query/analyze` | 无状态全文分析（完整扫描，可带 offset） |
| GET  | `/diagnostics` | 最近诊断记录 |

所有响应统一信封：`success/status/request_id/reason/data/error`。
可经 `X-Request-Id` 头传入请求标识；不传则自动生成。

---

## 5. 诊断与脱敏

- 每个接受/拒绝/无法判定的决策都写一条结构化记录：`request_id、outcome、
  operation、reason、state`（版本、偏移、窗口、块计数、重扫字符数等），
  同时进入标准库 `logging`，解释**为什么**接受、拒绝或无法判定。
- 文档正文**永不**进入日志。需要上下文时用 `redact_snippet`：只保留括号
  标点，其它字符折叠为点，引号/反斜杠替换为 `?`。测试
  `test_match_non_structural_offset_rejected_with_redaction` 用一段
  “口令”文本断言它既不出现在响应也不出现在诊断记录里。

---

## 6. 依赖版本（已在本机验证）

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.3（要求 ≥ 3.10） | 运行时 |
| fastapi | 0.141.1 | HTTP 框架 |
| pydantic | 2.13.5 | 模型校验 |
| uvicorn | 0.54.0 | 本地服务器 |
| pytest | 9.1.1 | 测试 |
| httpx | 0.28.1 | TestClient 传输（仅测试期） |
| SQLite | Python 标准库 sqlite3（WAL 模式） | 持久化 |

无 pydantic-settings 等额外依赖：配置项很少，`config.py` 直接解析环境变量
（`BRACKET_INDEX_DB`、`BRACKET_INDEX_CHUNK_SIZE`、`BRACKET_INDEX_LOG_LEVEL`、
`BRACKET_INDEX_REDACT_SNIPPETS`）。

## 7. 测试状态（如实标注）

- **已运行且通过：126 个 pytest 用例**（`126 passed`），覆盖率 95%。
- **未运行/未通过：无**（本机无 ruff/black/mypy/bandit，故未执行静态检查；
  已用 `python3 -m py_compile` 对全部模块做编译校验通过）。如需这些检查：
  `pip install ruff mypy bandit` 后运行 `ruff check app tests`、
  `mypy app`、`bandit -r app`。
- TestClient 有一条上游 `StarletteDeprecationWarning`（httpx 集成更名），
  不影响功能与断言。
