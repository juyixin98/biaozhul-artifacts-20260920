# 契约兼容性检查（contractcheck）

一个纯后端的本地 HTTP 服务：对**有限 JSON Schema 子集**描述的请求/响应契约做新旧版本兼容性检查，并附带一个**故障注入 HTTP 客户端**与进程内假服务，用于在完全不接触生产系统的前提下演练超时、连接错误、坏状态码、坏响应体与重试退避。时钟可控，结果结构化（JSON）。

- 语言：Go 1.22，标准库实现，**零第三方依赖**
- 仅监听本地回环（默认 `127.0.0.1:8080`）
- 无前端、无数据库、无任何外部网络调用

## 1. 兼容性模型（核心约定）

兼容性检查统一归约为一次**“生产者合法值集 ⊆ 消费者接受值集”**的子集判定：

| 方向 | 含义 | 生产者 / 消费者 | 允许的演化 |
|------|------|----------------|-----------|
| `request`（请求放宽） | 旧客户端发出的请求，新服务端必须仍能接受：`valid(old) ⊆ valid(new)` | 生产者=old，消费者=new | 新契约只能**放宽**（如必填改可选、枚举扩容、范围放宽） |
| `response`（响应收紧） | 新服务端产出的响应，旧消费者必须仍能处理：`valid(new) ⊆ valid(old)` | 生产者=new，消费者=old | 新契约只能**收紧**（如枚举收窄、范围收窄、保证更多必填字段） |

每发现一处“生产者允许、但消费者拒绝”的情况，就输出一条 finding，含：

- `path`：JSON Pointer 风格字段路径（`""` 为根）
- `kind`：`type` / `enum` / `minimum` / `maximum` / `minLength` / `maxLength` / `required` / `items` / `additionalProperties`
- `message`：人类可读说明
- `example`：一个**生产者合法但消费者拒绝的示例值**（反例）

### 三态结论

- `compatible`：未发现反例
- `incompatible`：存在反例，`findings` 非空
- `unknown`：任一侧出现**本实现不支持的 schema 关键字**——无法保证结论正确，**绝不误判为通过**；`unknownKeywords` 给出所在 side/path/keyword

### 支持的 Schema 子集

- `type`：字符串或数组，支持 `string` / `number` / `integer` / `boolean` / `object` / `array` / `null`（`integer` 视为 `number` 子类型）
- `enum`
- 对象：`properties`、`required`、`additionalProperties`（布尔或 schema）
- 数值：`minimum`、`maximum`
- 字符串：`minLength`、`maxLength`
- 数组：`items`
- 布尔 schema `true` / `false`
- 注解关键字安全忽略：`$schema`、`title`、`description`、`default`、`examples`

其余关键字（`$ref`、`const`、`format`、`pattern`、`multipleOf`、`oneOf`、`allOf`、`anyOf`、`if/then/else`、`uniqueItems` 等）一律记为未知 → 结论 `unknown`。

> 说明：本项目做的是**静态契约子集判定**，不是完整 JSON Schema 实例校验器；类型组合、跨约束交互只在上述子集范围内保守处理。

## 2. 目录结构

```
cmd/server          HTTP 服务入口
cmd/probe           故障注入客户端的命令行演示
internal/clock      可控时钟（Real / Fake）
internal/schema     Schema 子集解析、规范化、未知关键字探测
internal/compat     兼容性核心（方向语义 + 子集检查 + 反例生成）
internal/faultclient 带指数退避重试的客户端 + 进程内假 HTTP 服务
internal/registry   进程内假契约仓库（“外部依赖”的替身）
internal/httpapi    HTTP 路由与请求/响应结构
examples/contracts  新旧契约反例样例
examples/requests   HTTP 请求样例
```

## 3. 快速开始

```bash
go build ./...
go test ./...                 # 全部自动化测试
go run ./cmd/server           # 启动服务（默认 127.0.0.1:8080）
```

## 4. HTTP API

### `GET /healthz`
健康检查。

### `POST /v1/contracts`
把契约存入进程内假仓库（重启即失）。

```json
{"name": "user-request", "version": "v1", "schema": {"type": "object"}}
```

### `GET /v1/contracts`
列出已注册契约。

### `POST /v1/compat/check`
兼容性检查。`old` / `new` 每侧二选一：内联 `"schema"`，或仓库引用 `"name"+"version"`。

```json
{
  "direction": "request",
  "old": {"name": "user-request", "version": "v1"},
  "new": {"name": "user-request", "version": "v2"}
}
```

### `POST /v1/probe`
起一个进程内假服务（按 `fault.kind` 注入故障），用故障注入客户端调用并返回结构化的逐次尝试记录。

```json
{
  "fault": {"kind": "flaky", "flakyTimes": 2},
  "client": {"maxAttempts": 4, "initialBackoffMs": 10, "maxBackoffMs": 100, "useFakeTime": true}
}
```

故障类型 `none|delay|status|badbody|close|flaky`；`useFakeTime:true` 时退避由可控时钟推进，不占真实时间。重试策略：传输错误、`429`、`5xx`、`200 但响应体非法` 重试，`4xx` 不重试，退避指数增长并设上限。

## 5. 故障注入客户端 CLI

```bash
go run ./cmd/probe -fault flaky -flaky-times 2 -attempts 4 -fake-time
go run ./cmd/probe -fault status -status 503 -attempts 2
go run ./cmd/probe -fault badbody
go run ./cmd/probe -fault close
```

成功退出码 `0`；调用最终失败退出码 `2`，stdout 仍输出结构化结果。

## 6. 验收：新旧契约反例

样例契约见 `examples/contracts/`。

### 请求方向 `user-request v1 → v2`（`v2` 收紧，期望 incompatible）

v2 相对 v1：新增必填 `email`、`age.minimum` 由 0 提到 18、`name` 长度由 1..64 收到 2..32、`role` 枚举移除 `guest`。

```bash
curl -s -X POST http://127.0.0.1:8080/v1/compat/check \
  -d @examples/requests/check-request-direction.json
```

输出（节选）——每条都带**不兼容字段路径与示例值**：

```json
{
  "direction": "request",
  "status": "incompatible",
  "findings": [
    {"path": "", "kind": "required", "message": "消费者要求必填字段 \"email\"，但生产者不保证提供", "example": {}},
    {"path": "/properties/age", "kind": "minimum", "message": "生产者允许小于消费者下限 18 的值", "example": 17},
    {"path": "/properties/name", "kind": "minLength", "message": "...长度小于消费者下限 2...", "example": "x"},
    {"path": "/properties/name", "kind": "maxLength", "message": "...长度大于消费者上限 32...", "example": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
    {"path": "/properties/role", "kind": "enum", "message": "生产者枚举值 guest 不在消费者枚举 [admin user] 中", "example": "guest"}
  ]
}
```

### 响应方向 `user-response v1 → v2`（`v2` 放宽产出，期望 incompatible）

v2 不再保证必填 `name`、`status` 枚举增加 `banned`、`score.maximum` 由 100 放到 1000：

```json
{
  "direction": "response",
  "status": "incompatible",
  "findings": [
    {"path": "", "kind": "required", "message": "消费者要求必填字段 \"name\"...", "example": {}},
    {"path": "/properties/score", "kind": "maximum", "message": "...大于消费者上限 100...", "example": 101},
    {"path": "/properties/status", "kind": "enum", "message": "生产者枚举值 banned 不在消费者枚举 [active suspended deleted] 中", "example": "banned"}
  ]
}
```

### 不支持关键字（期望 unknown，而非通过）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/compat/check \
  -d @examples/requests/check-unknown-keyword.json
```

```json
{
  "direction": "request",
  "status": "unknown",
  "findings": [],
  "unknownKeywords": [{"side": "new", "path": "", "keyword": "pattern"}]
}
```

## 7. 实际运行记录（2026-09-25，Go 1.22.2，linux/amd64）

环境与过程如实记录。开发中发现并修复的问题，以及最终结果如下。

**构建与静态检查**

| 命令 | 结果 |
|------|------|
| `go vet ./...` | 通过，无输出 |
| `gofmt -l .` | 通过，无输出（开发中曾有 4 个文件未格式化，已 `gofmt -w` 修正） |
| `go build ./...` | 通过 |
| `go test -race -count=1 ./...` | 全部通过，无数据竞争 |

**自动化测试与覆盖率**（`go test ./... -count=1 -cover`）

| 包 | 覆盖率 |
|----|--------|
| internal/clock | 100.0% |
| internal/compat | 86.0% |
| internal/faultclient | 90.8% |
| internal/httpapi | 80.7% |
| internal/registry | 96.2% |
| internal/schema | 94.9% |

`cmd/server`、`cmd/probe` 为薄入口（flag 解析 + 组装），覆盖率不统计；业务逻辑均在可测的 internal 包内。

**真实 HTTP 端到端**（构建二进制 → 起服务于 127.0.0.1:18080 → curl）

1. 注册 4 份样例契约：均返回 `201 stored`。
2. 请求方向 v1→v2：`incompatible`，输出 required/minimum/minLength/maxLength/enum 共 5 条带示例值的 finding。
3. 响应方向 v1→v2：`incompatible`，输出 required/maximum/enum 共 3 条带示例值的 finding。
4. 含 `pattern` 的 schema：`unknown`，列出 unknownKeywords。
5. `flakyTimes=2` 故障注入：前 2 次 500、第 3 次 200，`success:true`，退避序列 10ms/20ms，可控时钟记录 `elapsedMs:30`。
6. 持续 500：`success:false`，CLI 退出码 `2`。

**开发过程中发现并修复的问题（无遗留未通过项）**

1. 时钟测试中复合字面量直接调方法 `Real{}.Now()` 触发 Go 语法错误 → 改为 `(Real{}).Now()`。
2. 补充测试暴露出真实逻辑缺陷：双方都用 schema（非布尔）声明 `additionalProperties` 时未做递归比较，会漏判不兼容 → 已重构为统一的 `checkAdditionalProperties`，并补回归测试。
3. 无反例时 `findings` 序列化为 `null` 而非 `[]` → 初始化为空切片。

## 8. 边界与非目标

- 不实现完整 JSON Schema；`$ref` 解析、条件约束、字符串 `pattern/format`、`oneOf` 组合等一律走 `unknown` 而不是猜测。
- 不接生产系统：契约仓库是进程内内存，依赖服务是 `httptest` 假服务。
- 不做鉴权、持久化、前端；仅用于本地契约演化分析与客户端韧性演练。
