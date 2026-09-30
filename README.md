# decorrelate

受限相关子查询的**去相关（decorrelation / unnesting）编译与执行服务**。

技术栈：Rust · [Axum](https://github.com/tokio-rs/axum)（HTTP）·
[arrow2](https://crates.io/crates/arrow2)（类型化列式批次）。
全部数据来自本地合成夹具或请求内联关系，无任何生产账号或外部业务系统依赖。

每个请求都会被**两套执行策略**各执行一次：

1. `naive_rowwise` —— **参考语义**：逐外层行嵌套循环解释执行（字面相关语义）；
2. `decorrelated_hash_pipeline` —— **去相关执行**：对内表按相关键一次建哈希组，
   再以 `LEFT OUTER` 哈希探测 + 分组归约。

只有当两者的**结果完全一致**（或在同一外层行以同一错误类别/码失败）时才返回；
任何分歧都会以 `EquivalenceMismatch` 失败关闭（fail-closed）。

---

## 1. 支持的子查询形式

| `type`          | SQL 形态                                   | 结果类型           |
|-----------------|--------------------------------------------|--------------------|
| `exists`        | `EXISTS (SELECT ... WHERE o.k = i.k ...)`  | Boolean            |
| `not_exists`    | `NOT EXISTS (...)`                         | Boolean            |
| `scalar_agg`    | `(SELECT COUNT(\*)/COUNT(c)/SUM(c) ...)`   | Int64（COUNT）/ 参数类型（SUM） |
| `scalar`        | `(SELECT col ...)`                         | 内表列类型         |
| `not_in`        | `x NOT IN (SELECT ...)`                    | **拒绝**，见下     |

相关条件必须是一个或多个 **等值合取** `outer_col = inner_col`，且两侧列类型完全相同
（不做隐式跨类型提升）。不支持无相关子查询、非等值谓词、其它聚合函数、表达式等，
会以 `UnsupportedForm` / `Validation` 拒绝。

### 关键语义（均有测试固定）

- **保留外层重复行与行序**：外层表从不去重、不重排；哈希组以“外层行地址”为键追加列，
  两条键值相同的外层行会各自得到相同但独立的结果。
- **SQL 三值逻辑等值**：任一键为 NULL，相关谓词不为真，该行不匹配（`NULL = NULL` 也不行）；
  内表含 NULL 键的行在哈希建组时被排除，任何外层行都探测不到。
- **空相关组上 COUNT 与 SUM 的区别**：
  - `COUNT(*)` / `COUNT(col)` 对空组为 **0**（重写 SQL 中显式 `COALESCE(..., 0)`）；
  - `SUM(col)` 对空组（以及组内值全为 NULL）为 **NULL**（重写中**故意没有** COALESCE）；
  - `COUNT(col)` 在非空组内仍跳过 NULL 值。
- **标量子查询多行必报错**：某外层行匹配到多于 1 行时，两套策略都在该行返回
  `CardinalityViolation / cardinality_violation`（HTTP 422，`verified_error`）；
  0 行 → NULL；恰好 1 行 → 该值（值本身为 NULL 时结果也是 NULL）。
- **NOT IN ≠ NOT EXISTS**：当子查询值中出现 NULL，`x NOT IN (SELECT ...)` 对所有外层行
  都是 UNKNOWN（永不为真），而 NOT EXISTS 使用普通相关等值；二者 NULL 语义不同，
  本服务在 `validate` 与 `rewrite` **两道关卡**都以
  `UnsupportedNotIn / not_in_unsupported`（HTTP 400）拒绝，绝不执行。

### 去相关重写与适用性证明

响应的 `rewrite` 段对每个子查询给出：

- `sql_rewrite`：去相关后的 SQL 形态（`LEFT JOIN` 分组派生表；COUNT 带 COALESCE、SUM 不带）；
- `physical_steps`：算子序列 `scan → hash_build → left_outer_hash_join → group_aggregate
  → mark/scalar_project`；
- `applicability_checks`：逐项适用性前置条件及通过理由；
- `proof`：形式化论证束（P1 外层多重性、P2 SQL 等值、P3 标记、P4 空组 COUNT/SUM、
  P5 标量基数、P6 多子查询复合），并列出证明所依赖的全部假设（即支持面边界）。

---

## 2. 工程结构（多模块，各自承担实际工作）

```
src/
├── batch/            类型化批次：arrow2 数组 + 显式 ColType/Datum，JSON 边界转换
├── query/
│   ├── mod.rs        逻辑模型、相关描述、错误类别体系、SQL 等值/类型检查
│   ├── plan.rs       名字/类型已解析的 ValidatedQuery（仅位置下标）
│   └── rewrite.rs    去相关重写：物理算子、SQL 渲染、适用性检查与证明
├── catalog/          资源与状态：合成夹具目录装载、请求内联覆盖（不污染共享状态）
├── exec/
│   ├── operators.rs  哈希内表组 + 统一归约规则（COUNT/SUM/标记/基数检查）
│   ├── naive.rs      逐行嵌套循环参考解释器
│   └── pipeline.rs   去相关哈希流水线
├── api/
│   ├── validate.rs   验证入口：解析关系/列名、类型检查、拒绝未支持形式
│   ├── verify.rs     双策略结果/错误等价核验（失败关闭）
│   ├── service.rs    编排 validate→rewrite→execute×2→verify，组装可解释响应
│   ├── http.rs       Axum 路由、X-Request-Id 关联、结构化日志
│   └── types.rs      请求/响应 DTO
├── config.rs         本地配置（config/default.json + DECORRELATE_* 环境变量）
└── main.rs           服务入口
fixtures/tables/      本地合成夹具（customers / orders / empty_log / scalar_notes）
examples/             可直接 curl 的请求样例
tests/                独立集成测试 + 独立第三方 JSON 参考实现（不引用被测执行核心）
```

没有“硬编码演示”路径：所有结果都由校验 → 重写 → 算子执行真实计算得出。

---

## 3. 构建与运行

依赖版本已在 `Cargo.toml` 中以 `=` 精确锁定，并提交 `Cargo.lock`。

```bash
cargo build --release
./target/release/decorrelate-server --config config/default.json
# 或直接 cargo run
cargo run
```

配置（`config/default.json`，均可用 `DECORRELATE_BIND` / `DECORRELATE_FIXTURES_DIR` /
`DECORRELATE_LOG_FILTER` / `DECORRELATE_LOG_JSON` 覆盖）：

```json
{ "bind": "127.0.0.1:8080", "fixtures_dir": "fixtures/tables",
  "log_filter": "info,decorrelate=debug", "log_json": true }
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz | jq
```

### 示例调用

```bash
# EXISTS / NOT EXISTS（含外层 NULL 键、重复键、夹具中的空相关行）
curl -s -X POST http://127.0.0.1:8080/v1/query \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-1' \
  --data @examples/01-exists.json | jq '.results.final_rows.rows'

# COUNT(*)/COUNT(col)/SUM 对比，含空内表 empty_log（空组 0 vs NULL）
curl -s -X POST http://127.0.0.1:8080/v1/query \
  -H 'Content-Type: application/json' \
  --data @examples/02-count-sum.json | jq '.results.final_rows.rows'

# 标量子查询多行反例：HTTP 422 verified_error，两套策略同类别同外层行失败
curl -s -o /tmp/sc.json -w '%{http_code}\n' -X POST http://127.0.0.1:8080/v1/query \
  -H 'Content-Type: application/json' \
  --data @examples/03-scalar-multirow.json
jq '.error, .results.equivalent' /tmp/sc.json

# NOT IN 被拒绝：HTTP 400 UnsupportedNotIn（绝不执行）
curl -s -o /tmp/ni.json -w '%{http_code}\n' -X POST http://127.0.0.1:8080/v1/query \
  -H 'Content-Type: application/json' \
  --data @examples/04-not-in-rejected.json
jq '.error' /tmp/ni.json
```

请求既可用 `{"relation": "customers"}` 引用夹具，也可用
`{"name": ..., "schema": [...], "rows": [...]}` 完全内联；顶层 `relations`
数组中的内联表只对当前请求覆盖夹具，不写回共享目录。

---

## 4. 可解释性（接口结果与日志）

- 每个响应与每条日志都带 **request id**：请求头 `X-Request-Id` 原样回显；
  缺省时服务端生成 `req_<uuid>`。
- `steps` 依次列出 `validate / rewrite / execute / verify` 的状态、耗时事件与关键计数
  （建组数、被排除的 NULL 键行数、双策略名称）。
- 失败原因单列于 `error`：`category`、稳定 `code`、人类可读 `message`、
  `at_step`（如 `exec:decorrelated_hash_pipeline`）、`at_outer_row` 与结构化 `detail`。
- 两套策略的列结果与执行轨迹分别在 `results.naive` / `results.decorrelated` 中展示，
  `equivalent` 明确给出核验结论；`service_version` 随每个响应返回。
- **不确定结论单列**：成功响应的 `uncertainties` 显式声明浮点累加顺序、NaN 限制等；
  双策略都失败但彼此一致时状态为 `verified_error`（结果可解释、错误经核验）。
- 日志默认 JSON 行格式，字段包含 `request_id / step / status / category / code /
  outer_row / elapsed_us`。

错误类别与 HTTP 状态：

| category               | 状态 | 含义                                             |
|------------------------|------|--------------------------------------------------|
| Validation             | 400  | 结构/名字/形状错误、JSON 非法                    |
| UnsupportedNotIn       | 400  | NOT IN，NULL 语义不等价，拒绝                    |
| UnsupportedForm        | 400  | 支持面之外（无相关、非等值等）                   |
| TypeMismatch           | 400  | 相关键/SUM 参数类型不兼容                        |
| DataError              | 400  | 夹具/内联数据违反声明模式                        |
| CardinalityViolation   | 422  | 标量子查询对某外层行返回多行                     |
| NumericOverflow        | 422  | Int64 SUM 溢出                                   |
| EquivalenceMismatch    | 500  | 双策略分歧（失败关闭，不返回结果）               |
| Internal               | 500  | 内部不变量违背                                   |

---

## 5. 测试与验证

```bash
cargo test                 # 全部单元 + 集成测试
cargo clippy --all-targets -- -D warnings
cargo fmt --check
```

测试**断言具体结果值与失败类别**，不是“接口能调用”：

- 外层 NULL 键、重复外层键（行序与重复度）、空内表、内表 NULL 键；
- 空组 `COUNT=0` 与 `SUM=NULL`、非空组 `COUNT(col)` 跳过 NULL；
- 多行标量反例：两套策略在同一外层行以 `cardinality_violation` 失败（422）；
- NOT IN 拒绝类别与所在阶段、无相关/类型不匹配/未知名列等拒绝路径；
- Int64 SUM 溢出的失败类别一致性。

**参考答案不由被测核心自身生成**：`tests/common/mod.rs` 中的 `oracle::evaluate`
是独立的第三实现——直接读取 JSON 行、手写嵌套循环与 SQL NULL 语义，
不 import 任何生产执行模块（`exec` / `query::rewrite` 等）；
`oracle_cross_check_*` 用它逐子查询比对生产输出。另有真实 TCP 上的 Axum 端到端测试
（`tests/api_http_test.rs`，不引入 HTTP 客户端依赖，手写最小 HTTP/1.1 收发）。

---

## 6. 剩余限制（明确声明）

- 相关谓词仅限等值合取；不支持 `<`、`BETWEEN`、OR、表达式与函数。
- 类型必须逐列一致；不做 Int64/Float64 隐式互转。
- 聚合仅 `COUNT(*)`、`COUNT(col)`、`SUM(col)`；无 `MIN/MAX/AVG`、`DISTINCT`、`GROUP BY`
  下推到外层以外的形态。
- Float64 SUM 按内表行序累加，浮点舍入因此与顺序相关（在 `uncertainties` 中声明）；
  NaN 不作为值/分组键支持。
- 服务为单进程内存执行，无持久化、鉴权与并发写；夹具是本地合成数据。
- 相关列只引用原始外层列，子查询结果列不参与后续相关（P6）。
