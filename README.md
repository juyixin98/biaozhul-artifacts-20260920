# contractcheck — 契约兼容性检查服务

纯后端 Go 项目：本地 HTTP 服务，对新旧两版服务契约做**请求/响应双向兼容性检查**，
输出结构化结果（不兼容字段路径 + 反例值）。所有外部依赖均为**本进程假服务**，
不接触任何生产系统；时间通过可控时钟抽象，故障通过注入式 HTTP 客户端模拟。

## 功能

- 有限 JSON Schema 子集的兼容性判定：
  - 支持关键字：`type`、`properties`、`required`、`enum`、`items`、
    `minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum`
  - 注解关键字（`$schema`、`title`、`description`、`default`、`examples`）被忽略
  - **任何其他关键字（如 `pattern`、`format`、`minLength`、`additionalProperties`、
    `$ref`、`oneOf` …）→ 检查结果为 `unknown`，绝不误判为通过**
- 方向语义：
  - **请求方向（request）**：新契约必须接受旧契约的一切请求（old ⊆ new），
    即"请求放宽"兼容、"请求收紧"不兼容
  - **响应方向（response）**：新契约产生的响应必须落在旧契约允许范围内
    （new ⊆ old），即"响应收紧"兼容、"响应放宽"不兼容
- 每条不兼容记录给出 JSON 路径（如 `$.user.age`、`$.items[*]`）、原因和**反例值**
- 故障注入客户端：对注册中心调用注入延迟与错误（启动参数或逐请求头控制）
- 可控时钟：`clock.Clock` 接口，测试用手动时钟，报告时间戳可复现
- 状态优先级：`incompatible` > `unknown` > `compatible`

## 结构

```
cmd/contractcheckd/     服务入口（同时拉起本进程假注册中心）
internal/schema/        JSON Schema 子集解析，收集不支持的关键字
internal/compat/        双向兼容性判定与反例生成
internal/clock/         可控时钟（Real / Fake）
internal/registry/      本进程假契约注册中心（内存存储 + HTTP）
internal/fault/         故障注入 HTTP RoundTripper（延迟 / 错误率）
internal/api/           检查服务 HTTP 接口
examples/               请求样例与端到端脚本
```

## 运行

```bash
go build -o /tmp/contractcheckd ./cmd/contractcheckd
/tmp/contractcheckd -addr 127.0.0.1:28080 -registry-addr 127.0.0.1:29001
```

启动参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-addr` | `127.0.0.1:8080` | 检查服务监听地址 |
| `-registry-addr` | `127.0.0.1:9001` | 本进程假注册中心监听地址 |
| `-fault-latency` | `0` | 注入到每次注册中心调用的延迟 |
| `-fault-error-rate` | `0` | 注册中心调用注入错误的概率 [0,1] |

服务内置样例契约 `orders@v1` / `orders@v2`，v2 在两个方向上各有多处不兼容。

## API

### `POST /v1/compatibility/check`

从注册中心取两个版本的契约进行比较。请求样例见 `examples/check-orders.json`：

```json
{"service": "orders", "fromVersion": "v1", "toVersion": "v2"}
```

响应（节选，完整输出见下文"实测记录"）：

```json
{
  "service": "orders", "fromVersion": "v1", "toVersion": "v2",
  "checkedAt": "2026-09-25T10:28:19Z",
  "status": "incompatible",
  "request": {
    "direction": "request", "status": "incompatible",
    "incompatibilities": [
      {"path": "$", "reason": "field \"currency\" is now required",
       "counterexample": {"amount": 0.5, "orderId": "string"}},
      {"path": "$.currency", "reason": "enum value CNY was removed by the new contract",
       "counterexample": "CNY"},
      {"path": "$.amount", "reason": "new contract raises the lower bound to inclusive 1",
       "counterexample": 0}
    ]
  },
  "response": {
    "direction": "response", "status": "incompatible",
    "incompatibilities": [
      {"path": "$.status",
       "reason": "new contract may return enum value pending, which the old contract did not allow",
       "counterexample": "pending"},
      {"path": "$.etaDays",
       "reason": "new contract may return values above the old upper bound (inclusive 30)",
       "counterexample": 60}
    ]
  }
}
```

逐请求故障注入（请求头会透传到注册中心调用）：

```bash
curl -X POST $BASE/v1/compatibility/check \
  -H 'X-Fault-Error-Rate: 1' -H 'X-Fault-Latency-Ms: 50' \
  -d @examples/check-orders.json
# => 502 {"error":"fetching old contract: registry returned status 503"}
```

### `POST /v1/compatibility/checkInline`

不经过注册中心，直接在请求体里给出两份契约
（`old.request` / `old.response` / `new.request` / `new.response`）。
样例：`examples/check-inline.json`（不兼容）、
`examples/check-inline-unknown.json`（含不支持关键字，返回 `unknown`）。

### `GET /healthz`

返回 `{"status":"ok"}`。

## 测试

```bash
go vet ./...
go test -cover ./...
./examples/run.sh    # 端到端冒烟：构建、起服务、跑 5 个场景
```

## 实测记录（2026-09-25，go1.22.2 linux/amd64）

- `go vet ./...`：通过，无输出。
- `go test -cover ./...`：**全部通过，无失败项**。覆盖率：
  api 88.5%、compat 84.3%、fault 95.7%、registry 100%、schema 96.1%、clock 100%。
- `./examples/run.sh` 五个场景实际输出：
  1. `GET /healthz` → `{"status":"ok"}`
  2. 注册中心检查 orders v1→v2 → `status: "incompatible"`，请求方向 3 条
     （新增必填 `currency`、枚举删除 `CNY`、`amount` 下限 0→1），
     响应方向 2 条（枚举新增 `pending`、`etaDays` 上限 30→60），均带反例值
  3. inline 检查同一对契约 → 结果与场景 2 一致
  4. 含 `minLength`/`format` 的契约 → `status: "unknown"`，
     `unknowns` 列出对应关键字，**未误判为通过**
  5. `X-Fault-Error-Rate: 1` → `502 {"error":"fetching old contract: registry returned status 503"}`

### 开发中发现并已修复的问题

- 首次端到端运行时默认端口 8080 及后续 18080 均被本机其他进程占用
  （`bind: address already in use`），冒烟脚本改用 28080/29001。
- 初版响应方向的原因文案沿用了请求方向措辞（把"新契约新增枚举值"误述为
  "被移除"），已改为按方向生成文案。
- 初版故障注入头只作用于出站请求、未从入站请求透传，场景 5 首次运行未生效；
  已在 `api.fetchContract` 中透传 `X-Fault-*` 头并补测试。

## 已知限制

- 仅实现上述关键字子集；`additionalProperties` 未建模，对"仅一侧声明的
  属性"采取宽松处理（只有 `required` 变化会判不兼容），数组 `items` 仅在
  两侧都声明时递归比较。
- 反例为代表性示例值，不保证是完整 witness 集合。
- 假注册中心为内存存储，重启即丢失。
