# licensejudge — SPDX 风格许可证表达式判定（纯后端）

一个**纯本地、无前端、不连接任何云平台**的 Go 后端服务：解析 SPDX 风格许可证
表达式子集（`AND` / `OR` / `WITH` / 括号），按用户自定义策略做三值判定
（`allow` / `deny` / `unknown`），输出**满足策略的一条选择**或**拒绝/未知原因**，
并保留 OR 的全部备选分支。

> ⚖️ **非法律声明**：本工具只做"表达式 × 策略配置"的机械判定，
> **不构成法律意见**，不判断某许可证在具体场景下的真实义务。
> 策略中出现的 GPL/AGPL/SSPL 等取舍均为**虚构示例**，不代表对这些许可证的评价。

---

## 1. 能力与边界

### 支持的表达式子集

```
orExpr  := andExpr (OR andExpr)*
andExpr := atom (AND atom)*
atom    := license (WITH exception)? | '(' orExpr ')'
```

- 运算符（大写，大小写敏感）：`AND`、`OR`、`WITH`
- 优先级：`WITH`（绑单个原子）> `AND` > `OR`；括号可改变分组
- 标识符：SPDX 风格 ID（`MIT`、`Apache-2.0`、`LGPL-2.1-only`）、
  `LicenseRef-*`、`DocumentRef-x:LicenseRef-y`
- 支持 `LGPL-2.1-only WITH Classpath-exception-2.0` 这类例外组合

### 显式不支持

- SPDX 的 `GPL-2.0+` **加号后缀运算符**（遇到返回带位置的错误）
- SPDX 许可证列表版本校验、`-or-later` 语义归一化（按字面 ID 匹配策略）
- 任何网络/云端许可证库查询——**全部依据本地策略文件**

### 三值判定语义（Kleene 逻辑）

| 组合 | 结果 |
|---|---|
| `AND`：任一 `deny` | `deny`（拒绝优先） |
| `AND`：无 deny 但有 `unknown` | `unknown` |
| `AND`：全部 `allow` | `allow` |
| `OR`：任一 `allow` | `allow`（允许优先），并选**最左**可满足分支为 `selection` |
| `OR`：无 allow 但有 `unknown` | `unknown` |
| `OR`：全部 `deny` | `deny` |

**未知许可证永远返回 `unknown`，不会自动通过。**
策略加载器会**直接拒绝**把未列出许可证配置成 `allow`（或 `deny`）的策略文件——
"未知即放行"在配置层面就无法生效；要拒绝某个许可证必须显式列出它。

### WITH 例外规则

1. 许可证被显式 `deny` → 组合 `deny`，例外不能翻盘；
2. 许可证 `unknown` → 组合 `unknown`（无论例外是否已配置，绝不自动通过）；
3. 许可证已知、例外未知 → 组合 `unknown`；
4. 两者均已知：例外可声明 `applies_to` 绑定清单，许可证不在清单内 → `deny`；
   再按例外自身的 allow/deny 裁决。

---

## 2. 目录结构

```
cmd/licensejudge/          服务与 CLI 入口（serve / eval）
internal/expression/       词法分析、递归下降解析器、AST、规范渲染
internal/policy/           自定义策略模型、校验、JSON 加载
internal/judge/            三值判定引擎（AND/OR/WITH、备选分支、原因）
internal/cache/            AST 磁盘缓存（原子写、版本校验、篡改即失效）
internal/server/           本地 JSON HTTP 接口 + 审计日志
configs/policy.json        示例自定义策略（虚构，非法律判断）
testdata/fixtures/         验收夹具（判定/解析/HTTP/命令清单，显式提供）
tests/                     由夹具驱动的验收测试
scripts/fixturetest/       只执行 commands.json 中显式命令的验收程序
examples/requests.http     12 组 HTTP 请求样例
reports/                   夹具验收运行结果（fixture-report.json）
.work/ .cache/             运行时工作目录与缓存目录（互相分离，git 忽略）
```

### 缓存与工作目录分离

- `--cache-dir`：只存可由原表达式确定性重建的解析 AST（SHA-256 命名、原子 rename、
  版本与内容双重校验，损坏/被篡改即视为未命中）。**删掉它不丢任何源数据。**
- `--work-dir`：每次请求一份 `job-*.json` 结果与 `audit.log`。
- 服务启动时强制校验两者**绝对路径互不嵌套**，重合即拒绝启动（有测试覆盖）。

---

## 3. 构建与运行

要求 Go 1.22+（开发环境实测 go1.22.2 linux/amd64），**无需任何第三方依赖**。

```bash
go build -o bin/licensejudge ./cmd/licensejudge

# 启动本地 HTTP 服务（默认只监听 127.0.0.1:8080）
bin/licensejudge serve --policy configs/policy.json \
  --addr 127.0.0.1:8080 --work-dir ./.work --cache-dir ./.cache

# 或命令行单次判定
bin/licensejudge eval --policy configs/policy.json 'MIT OR GPL-3.0-only'
```

`eval` 退出码：`0=allow`，`1=deny`，`3=unknown`，`2=用法或解析错误`。

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| GET | `/v1/policies` | 当前策略概要与已知许可证 ID |
| POST | `/v1/evaluate` | 判定 `{"expression": "..."}` |

请求样例见 [`examples/requests.http`](examples/requests.http)。

成功响应（节选）：

```json
{
  "job_id": "job-22c3fc616824c3a7",
  "expression": "MIT OR GPL-3.0-only",
  "cached": false,
  "result": {
    "verdict": "allow",
    "selection": "MIT",
    "reasons": ["OR 左侧允许，选择左侧分支即可满足策略", "许可证 MIT 被策略允许"],
    "alternatives": [
      {"expression": "MIT", "verdict": "allow", "reasons": ["许可证 MIT 被策略允许"]},
      {"expression": "GPL-3.0-only", "verdict": "deny", "reasons": ["许可证 GPL-3.0-only 被策略拒绝"]}
    ],
    "leaves": [ ... ],
    "disclaimer": "本结果仅为基于所给配置的自动化合规判定，不构成法律意见；……"
  }
}
```

错误响应：`{"error": "...", "code": "parse_error|missing_expression|invalid_json|...", "disclaimer": "..."}`。

---

## 4. 自定义策略文件

见 [`configs/policy.json`](configs/policy.json)。结构：

```json
{
  "name": "my-policy",
  "version": "1.0.0",
  "unlisted": "unknown",
  "licenses": {
    "MIT":          {"decision": "allow", "reason": "……"},
    "GPL-3.0-only": {"decision": "deny",  "reason": "……（虚构示例）"},
    "LicenseRef-X": {"decision": "unknown", "reason": "法务复核中"}
  },
  "exceptions": {
    "Classpath-exception-2.0": {
      "allow": true,
      "applies_to": ["LGPL-2.1-only", "GPL-2.0-only", "GPL-3.0-only"],
      "reason": "……"
    },
    "Bison-exception-2.2": {"allow": false, "reason": "……"}
  }
}
```

- 每个许可证必须显式给 `decision`：`allow` / `deny` / `unknown`；
- `unlisted` 只允许缺省或 `"unknown"`；配置成 `allow` 或 `deny` 会在加载时报错；
- JSON 中出现未知字段会被拒绝（`DisallowUnknownFields`）。

---

## 5. 自动化测试与验收

### 5.1 单元 / 集成测试

```bash
go test -count=1 ./...          # 全部测试
go test -race -count=1 ./internal/...
go vet ./...
```

### 5.2 夹具驱动验收（只运行显式提供的夹具命令）

夹具位于 `testdata/fixtures/`：

- `engine_cases.json` — 22 条判定用例（优先级、括号、AND/OR/WITH 例外组合、未知不通过）
- `parser_cases.json` — 15 条解析错误用例（缺括号、悬空运算符、`+`、小写关键字……）
- `api_cases.json` — 10 条 HTTP 用例（含缓存二次命中、400/405、非法律声明）
- `commands.json` — **显式批准执行的本地命令白名单**

`scripts/fixturetest` 只执行 `commands.json` 中列出的命令（go build/vet/test、
CLI 退出码），再在**内核临时分配端口**上启动真实子进程做 HTTP 冒烟，
结果写入 `reports/fixture-report.json`。它不联网，也不执行清单外的任何命令。

```bash
go run ./scripts/fixturetest -v
```

最近一次运行的实际结果见 [`reports/fixture-report.json`](reports/fixture-report.json)
与 [`RUNLOG.md`](RUNLOG.md)（命令、输出摘要、未通过项如实记录）。
