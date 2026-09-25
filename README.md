# licensejudge — 许可证表达式判定（纯后端）

一个本地运行的 Go 后端服务：解析 SPDX 风格许可证表达式子集
（`AND` / `OR` / `WITH`、括号、优先级），按**自定义许可策略**给出
配置层面的判定结果。未知许可证返回 `unknown` 而**不会自动通过**。

> ⚠️ 本工具只做**配置规则判定**，不构成任何法律意见或许可证兼容性结论。
> `deny` / `unknown` 的具体处置由使用方的合规流程决定。

## 特性

- **表达式子集**：支持 `license-id`、`(...)`、`A AND B`、`A OR B`、
  `license WITH exception`。优先级 `OR` < `AND` < `WITH`，运算符大小写不敏感。
- **三值、fail-closed 语义**：`allow` / `deny` / `unknown`。
  - 未在策略中声明的许可证或例外 → `unknown`，绝不自动放行。
  - `AND`：全部 allow 才 allow；任一 deny 即 deny；否则 unknown。
  - `OR`：任一 allow 即 allow，并返回**选中的备选分支**；全部 deny 才 deny；
    否则 unknown（保留全部备选分支及各自原因）。
  - `WITH` 例外：精确的“许可证 WITH 例外”组合可在策略中单独覆盖；
    无组合规则时要求许可证与例外**各自**被允许。
- **自定义策略**：JSON 配置，允许/拒绝许可证、例外及特定组合。
- **本地夹具命令**：服务只能执行清单中显式登记的本地命令（无 shell、
  无参数注入），用于触发构建/测试。
- **目录分离**：夹具的工作目录（`.local/work`）与结果缓存目录
  （`.local/cache`）是两个不同路径，缓存不污染工作树。
- **不连接任何云平台**，纯 `net/http` + 标准库，无第三方依赖。
- 仅 JSON 接口，**无前端**。

## 目录结构

```
cmd/licensejudge/        服务入口
internal/spdx/           表达式词法/语法解析、AST 与规范化打印
internal/policy/         自定义策略模型与 JSON 加载
internal/judge/          表达式判定引擎（三值逻辑、备选分支、原因）
internal/runner/         白名单夹具命令执行（work/cache 分离、缓存）
internal/api/            JSON HTTP 接口
configs/policy.json      示例策略
configs/fixtures.json    示例夹具命令清单
examples/                请求样例（curl）与真实响应样例
```

## 构建与运行

需要 Go 1.22+。

```bash
go build ./...
go run ./cmd/licensejudge \
  -addr 127.0.0.1:8080 \
  -policy configs/policy.json \
  -fixtures configs/fixtures.json \
  -work-dir .local/work \
  -cache-dir .local/cache
```

默认只监听 `127.0.0.1:8080`。启动时会打印实际使用的 work/cache 目录。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/healthz` | 健康检查 |
| `GET`  | `/v1/policy` | 查看当前加载的策略 |
| `POST` | `/v1/judge` | 判定一个许可证表达式 |
| `GET`  | `/v1/fixtures` | 列出可执行的白名单夹具命令及目录 |
| `POST` | `/v1/fixtures/run` | 执行一个已登记的夹具命令 |

### 判定请求

```json
POST /v1/judge
{ "expression": "GPL-3.0-only OR MIT AND Apache-2.0" }
```

响应（`decision=allow` 时 `selection` 是满足策略的那一条选择）：

```json
{
  "expression": "GPL-3.0-only OR MIT AND Apache-2.0",
  "decision": "allow",
  "selection": "MIT AND Apache-2.0",
  "alternatives": [ ... ]
}
```

拒绝/未知时返回 `reasons`（拒绝原因）并保留各备选分支：

```json
{
  "expression": "GPL-3.0-only OR AGPL-3.0-only",
  "decision": "deny",
  "reasons": [ ... { "code": "all-branches-denied" } ],
  "alternatives": [ ... ]
}
```

解析错误返回 `400` 与 `invalid-expression`。

### 夹具命令

```json
POST /v1/fixtures/run
{ "name": "go-test", "no_cache": false }
```

- 只有 `configs/fixtures.json` 中登记的名字可执行；其他名字返回
  `400 fixture-not-allowed`。
- 成功结果按“程序+参数”哈希缓存；再次调用返回 `"cached": true`。
- `no_cache: true` 强制重新执行。

## 策略文件格式

见 `configs/policy.json`。取值仅允许 `"allow"` / `"deny"`；缺省即 `unknown`。

```json
{
  "licenses":   { "MIT": "allow", "GPL-3.0-only": "deny" },
  "exceptions": { "Classpath-exception-2.0": "allow" },
  "combos":     { "GPL-2.0-only WITH Classpath-exception-2.0": "allow" }
}
```

`combos` 的键必须是 `"LICENSE WITH EXCEPTION"`，其判定优先于单独的
许可证/例外条目。

## 自动化测试

```bash
go test ./...            # 全部测试
go test ./... -cover     # 覆盖率
go vet ./...
```

测试覆盖：运算符优先级、括号分组、WITH 组合与非法表达式、
AND/OR 三值真值表、未知不自动通过、OR 备选分支保留与选中、
策略组合覆盖、夹具白名单拒绝、work/cache 目录分离与缓存命中等。

## 边界与非目标

- 不是 SPDX 全量实现：不支持 SPDX 许可证列表校验、`+` 后缀、
  `LicenseRef`/`DocumentRef`（可按需扩展解析器）。
- 不联网、不下载、不做签名校验、不做法律结论。
- 标识符按字面精确匹配（SPDX id 大小写敏感）；`AND/OR/WITH` 大小写不敏感。
