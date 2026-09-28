# 不可变有序词典 · 最小无环确定自动机（MADFA / DAWG）

从**有序词典**构建最小无环确定自动机，支持：

- **查词**（成员判定）：词是否被自动机接受；
- **前缀统计**：以某串为前缀的词典词数；
- **持久化**：构建结果原子写入 SQLite，读取时做引用完整性校验（环、悬空状态等）；
- **HTTP 服务**：FastAPI 提供构建与查询接口。

所有数据均为**本地合成夹具**（固定随机种子、命名小例子），不依赖任何生产账号
或真实业务数据。

---

## 1. 行为契约（这是正确性要求，不是演示功能）

1. **状态等价须同时比较终结标记和转移。**
   等价键为 `(final, sorted((symbol, target), ...))`。子节点数量相同但终结标记
   不同、出边符号不同或目标不同的状态不会被合并。见 `app/core/state.py`。

2. **新增输入若不有序，明确拒绝或先排序。**
   增量最小化只对字典序非递减输入正确。默认对逆序输入抛
   `unordered_corpus`（HTTP 422）；调用方可显式传 `sort_first=true` 由服务排序。
   相邻重复词不算逆序，规范化时去重并计数。

3. **空词支持规则固定。**
   默认**拒绝**空词（抛 `empty_word_rejected` / 查询抛
   `empty_query_rejected`）；只有显式声明 `allow_empty_word=true` 才接受，
   此时根状态为终结状态，空前缀计数包含空词本身。

4. **持久化引用校验避免环和悬空状态。**
   写入前、读取后都运行与存储无关的引用校验：根缺失、悬空边、不可达状态、
   环（含自环）、非确定转移（同源同符号两条边）、终结标记非法、词数缺失或
   与结构重算值不一致。任何违规都使操作失败（HTTP 500
   `index_integrity_violation`），**绝不返回成功**。见 `app/index/validator.py`。

### 前缀计数语义

在反向拓扑（真正的后序 DFS，跨层共享也成立）上计算：

```
count(s) = final(s) + Σ_{(sym,t) ∈ δ(s)} count(t)
```

两条边即使指向**同一个共享目标**也要各计一次——经不同符号到达的是不同字符串。
前缀路径中断（不可达）时计数为 `0`，响应显式带 `reachable=false`。

---

## 2. 工程组织（分层，可独立测试）

```
app/
├── corpus/      语料规范：词项校验、有序性、固定空词规则、合成夹具
│   ├── errors.py     明确错误类别（固定 error_code / http_status）
│   ├── spec.py       CorpusSpec 规范化（校验/排序/去重）
│   └── fixtures.py   本地命名夹具 + 确定性随机语料
├── core/        挖掘内核（不依赖持久化/网络）
│   ├── state.py      不可变状态 + 规范等价键（契约 1）
│   ├── dawg.py       有序增量 replace-or-register 最小化 + 不可变 Dawg
│   └── trie.py       独立参考 Trie（与 DAWG 无代码共享，用于差分）
├── index/       索引与模型
│   ├── validator.py  引用完整性校验（契约 4，与 SQLite 解耦）
│   ├── repository.py SQLite 原子读写 + PRAGMA 校验
│   ├── model.py      元数据/持久化索引视图
│   └── service.py    规范化→挖掘→校验→持久化 编排
├── query/       查询验证：输入校验、成员判定、前缀计数、显式结果类型
├── api/         FastAPI：create_app 工厂、薄路由、分离 schema、错误映射
├── config.py    独立配置层（环境变量覆盖，不可变）
└── diagnostics.py   运行身份 run_id 与文件日志

tests/
├── unit/        规范 / 状态等价 / DAWG / 参考Trie / 校验器 / 查询
├── integration/ SQLite 往返 + 存储级损坏注入（悬空/环/不可达/计数篡改）
├── api/         FastAPI 端到端（成功路径与各类错误）
├── contract/    四条契约的跨层专项测试
└── oracle.py    独立 Myhill–Nerode 划分细化最小化预言机（验证最小性）

run.py                 服务入口
scripts/demo.py        本地端到端演示（构建/查询/错误/差分/最小性）
scripts/run_tests.sh   一键测试 + 覆盖率
requirements.txt       依赖清单
```

### 为什么参考答案可信

- 语言与前缀计数用**独立实现** `ReferenceTrie`（朴素嵌套 dict）差分；
- 最小状态数用**第三种方法** `tests/oracle.py` 的划分细化（Myhill–Nerode）验证；
  二者都不调用被测内核，避免"参考答案全部由被测实现自己生成"。

---

## 3. 环境与安装

- Python 3.12（标准库 `sqlite3`，无需外部数据库）。

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt
```

依赖：`fastapi`、`uvicorn[standard]`、`pydantic v2`、`httpx`、`pytest`。

---

## 4. 运行服务

```bash
python3 run.py                 # 默认 127.0.0.1:8000
DAWG_PORT=8732 python3 run.py  # 自定义端口
```

可用环境变量：`DAWG_DATA_DIR`、`DAWG_DB_NAME`、`DAWG_LOG_DIR`、
`DAWG_LOG_LEVEL`、`DAWG_HOST`、`DAWG_PORT`、`DAWG_INDEX_NAME`。

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 版本、运行身份 run_id |
| POST | `/admin/build` | 用 `words` 或命名 `fixture` 构建索引 |
| GET  | `/stats` | 当前索引元数据；未构建时 `ready=false` |
| GET  | `/query/membership?word=...` | 查词 |
| GET  | `/query/prefix-count?prefix=...` | 前缀计数 |

构建请求体：

```json
{
  "words": ["cat", "cats", "dog"],
  "allow_empty_word": false,
  "sort_first": false,
  "fixture": null,
  "index_name": "my_index"
}
```

### curl 示例

```bash
# 用内置合成夹具构建
curl -s -X POST localhost:8000/admin/build \
  -H 'Content-Type: application/json' \
  -d '{"fixture":"shared_suffix"}'

curl -s "localhost:8000/query/membership?word=cats"
# {"success":true,"word":"cats","member":true,"status":"member", ...}

curl -s "localhost:8000/query/prefix-count?prefix=cat"
# {"success":true,"prefix":"cat","count":2,"reachable":true, ...}
```

---

## 5. 本地演示脚本

```bash
python3 scripts/demo.py
```

依次演示：构建持久化 → 重载查询（带判定依据）→ 固定错误类别 →
悬空/环校验 → 与参考 Trie 差分 → 最小性小例子。

---

## 6. 错误语义（固定 `error_code`，异常绝不伪装成功）

| error_code | HTTP | 触发 | 含义 |
|---|---|---|---|
| `unordered_corpus` | 422 | 构建 | 输入逆序且未允许排序 |
| `empty_word_rejected` | 422 | 构建 | 空词被固定规则拒绝 |
| `invalid_word` | 422 | 构建 | 词项非字符串 / 含代理码位 / 词表非列表 |
| `unknown_fixture` | 404 | 构建 | 命名夹具不存在 |
| `empty_query_rejected` | 422 | 查询 | 在不接受空词的索引上查空串 |
| `query_rejected` | 422 | 查询 | 查询输入非字符串 / 含代理码位 |
| `index_not_ready` | 503 | 查询 | 索引尚未构建（不是成功响应） |
| `index_integrity_violation` | 500 | 构建/加载 | 引用校验失败，`details` 列逐项类别与依据 |
| `metadata_missing` | 500 | 加载 | 数据库无索引元数据 |
| `internal_error` | 500 | 任意 | 未预期异常（同样 `success=false`） |

完整性违规的 `details` 元素形如 `"[dangling_edge] 边 0 --'Q'--> 999 指向不存在的悬空状态"`，
给出**判定依据**。注意：**非成员是正常结果**（HTTP 200、`success=true`、
`member=false`），与"请求被拒绝"（4xx）严格区分。

---

## 7. 测试与复现

```bash
# 全部测试（约 110+ 用例，带运行身份日志）
python3 -m pytest

# 一键：版本/依赖检查 + 测试 + 覆盖率
bash scripts/run_tests.sh
```

- 断言的是**具体结果与失败类别**（具体状态数、具体计数、具体 `error_code`），
  不是"接口能调用"。
- 验证材料覆盖：共享后缀、词为另一词前缀、重复词、空集合、空词、二进制小语料、
  确定性随机语料。
- 损坏注入在 SQLite 层直接构造（关外键插悬空边、插不可达状态、叶子回边成环、
  篡改 `word_count`），断言加载必然失败且类别正确。
- 日志写入 `logs/run-<run_id>.log`，记录 Python/pytest/FastAPI/应用版本、
  每个用例的 nodeid（参数化参数即输入身份）与判定依据，可关联到具体运行。
- 覆盖率（`pytest --cov=app`）当前 **≈96%**。

### 最小性示例（可手工核对）

| 词表 | 最小存活状态数 | 合并次数 |
|---|---|---|
| `[]` | 1（非终结根） | 0 |
| `[""]` | 1（终结根） | 0 |
| `cat cats dog dogs walk walks` | 10 | 4 |
| `be bee been beer bees` | 5 | 2 |
| `{0,1}` 上长度 0..4 全集（31 词） | 5 | 26 |

这些数字同时被 `tests/oracle.py` 的独立划分细化结果验证。

---

## 8. 设计说明

- **不可变**：`State`、`Dawg`、`CorpusSpec`、结果类型均为
  `frozen=True` dataclass；转移用只读 `MappingProxyType`。
- **构建器**只在构建期使用可变内部状态，`build()` 后产出不可变模型并重排紧凑
  状态 id（根固定 0）。
- **持久化**单事务 `BEGIN IMMEDIATE` 清空重写 + `PRAGMA foreign_keys` +
  `PRAGMA integrity_check`，失败回滚。
- **服务**为单索引模型；启动时若存在合法库则自动加载，库损坏时不阻止启动，
  查询返回 503。
