# 协议版本兼容网关 (Protocol Version Compatibility Gateway)

一个纯后端的本地 gRPC 网关，在两套遥测 schema 版本（**telemetry v1 ↔ v2**）之间做双向转换：
显式字段重命名、整数单位换算（带溢出与舍入检查）、枚举显式映射（新枚举不可表达时
**返回可定位错误或携带原值，绝不静默降级为 0**）。每条输出携带**来源版本 / 转换版本 /
所用映射及版本**的溯源信息；流式转换有**有界缓冲背压**，**取消即停止转换**；映射配置
存于 PostgreSQL 并支持**运行时热切换**；每条记录做真实的 **HMAC-SHA256 签名校验/重签**。

- 语言 / 框架：Go 1.22 + gRPC（`google.golang.org/grpc`）+ Protocol Buffers
- 存储：PostgreSQL（`github.com/jackc/pgx/v5`），存放映射配置 + 转换审计日志，热切换基于事务 + `LISTEN/NOTIFY`
- 密码学：标准库 `crypto/hmac` + `crypto/sha256`（真实执行，常量时间比较）

---

## 1. 两个版本的差异（全部使用显式映射）

| 语义 | v1（旧） | v2（新） | 映射 |
|---|---|---|---|
| 时间戳 | `timestamp_ms`（毫秒） | `observed_at_ns`（纳秒） | `ns = ms * 1_000_000`，反向 `/1_000_000` |
| 温度 | `temp_milli_c`（1/1000 °C） | `temperature_uk`（1/10⁶ K） | `uk = mc*1000 + 273_150_000`（仿射，含开尔文偏移），反向 `(uk-273_150_000)/1000` |
| 电量 | `battery_milli_pct`（1/1000 %），`optional` | `battery_pct_micro`（1/10⁶ %），`optional` | `micro = milli*1000`；**缺省与显式 0 严格区分并保留** |
| 枚举 | `status`：UNSPECIFIED/ACTIVE/IDLE/ERROR | `phase`：同上 + **`PHASE_MAINTENANCE(4)`** | 0–3 一一显式映射；**4 在 v1 无法表达** |
| 请求标识 | `request_id` | `trace_id` | 字段重命名 |
| 签名 | `signature`（v1 规范串 HMAC） | `signature`（v2 规范串 HMAC） | 入站验签，出站按目标版本重签 |

映射不是隐式约定，而是数据库中的 JSON 规格（`internal/mapping/spec.go`）：
字段表（`identity` / `int_unit`）+ 枚举表（`values` 数字到数字）+ 策略。

### 关键正确性保证

- **整数单位换算**：全程 `math/big.Int` 精确运算，`num/den/pre_add/post_add` 整数仿射变换；
  收窄到 `int64` 前检查上下界，各阶段（pre_add、乘法、post_add）溢出都返回 `INTEGER_OVERFLOW`。
  乘法/偏移方向（v1→v2，如 ms×10⁶→ns、mc×1000+偏移→uk）存在真实溢出；除法方向（v2→v1）
  int64 除以正整数数学上不可能越界，因此该方向由舍入策略把关。
- **舍入**：非整除时按策略处理——`REJECT`（默认，返回 `ROUNDING_REQUIRED`）、
  `NEAREST`（半数背离零，负数正确）、`FLOOR`（向负无穷）。
- **枚举扩展**：源枚举在目标表中缺失时（如 v2 的 4 → v1）：
  - `ERROR`（默认）：返回**可定位**错误，带字段名 `phase`、记录 ID、`original_enum=4`，gRPC `InvalidArgument`；
  - `CARRY_ORIGINAL`：目标枚举为 0，但把原值 **4** 写入 `raw_phase` 与溯源 `carried_original_enum`。
  两种方式都不会把 4 静默变成 `UNSPECIFIED(0)` 而不留痕。
- **缺省 vs 显式零**：`battery_*` 用 proto3 `optional`，缺省（`nil`）转换后仍缺省，
  显式 `0` 转换后是显式 `0`；规范签名对二者编码不同，密码学上也可区分。
- **未知字段**：入站消息里的未知 protobuf 字段不报错，计数进溯源 `unknown_fields_seen`，
  并在 wire 编解码往返中保留。
- **密码学真实执行**：每条记录按版本规范串（标签按 key 排序、分隔符转义）计算 HMAC-SHA256，
  入站用源版本密钥验签（`hmac.Equal` 常量时间比较），出站用目标版本密钥重签。

---

## 2. 目录结构

```
proto/telemetry/v1/telemetry.proto      # v1 schema
proto/telemetry/v2/telemetry.proto      # v2 schema（含新枚举 PHASE_MAINTENANCE）
proto/gateway/v1/gateway.proto          # 网关服务：unary + bidi 流 + 管理 RPC
gen/                                    # protoc 生成代码（已签入）
internal/mapping/                       # 映射规格、big.Int 单位换算、枚举映射、预置映射
internal/crypto/                        # HMAC-SHA256 规范签名/验签
internal/transform/                     # 记录级转换、presence/unknown 处理
internal/store/                         # PostgreSQL：迁移、种子、激活、审计、LISTEN/NOTIFY
internal/server/                        # gRPC 服务、有界背压流引擎、热切换注册表
internal/config/                        # 环境变量配置
cmd/gateway/                            # 网关服务端
cmd/demo/                               # 双版本客户端 demo
examples/                               # 示例输入 + 示例映射 JSON
scripts/initdb.sh                       # 本地 Postgres 角色/库初始化
test/                                   # gRPC + PostgreSQL 集成测试
internal/**/*_test.go                   # 单元测试（换算/舍入/溢出/枚举/签名/流引擎）
```

---

## 3. 本地启动

### 前置
- Go 1.22+、`protoc` + `protoc-gen-go`/`protoc-gen-go-grpc`、PostgreSQL（在 5432 运行）

```bash
# 1) 安装 proto 插件（一次性）
go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.34.2
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
export PATH="$PATH:$(go env GOPATH)/bin"

# 2) 拉取并锁定依赖（go.mod / go.sum 已签入）
go mod tidy

# 3) 准备 Postgres（创建 compgw 角色与 compgw / compgw_test 库；幂等）
make initdb

# 4)（如改动 proto）重新生成
make proto

# 5) 启动网关（自动迁移 + 种子映射）
make run
# 或：go run ./cmd/gateway
```

可选环境变量：`COMPGW_GRPC_ADDR`（默认 `127.0.0.1:50051`）、
`COMPGW_PG_DSN`（默认 `postgres://compgw:compgw@localhost:5432/compgw?sslmode=disable`）、
`COMPGW_MAX_IN_FLIGHT`（默认 64）、`COMPGW_HMAC_KEY`（demo 客户端用）。

启动后自动迁移建表并种子三个映射：`strict-v1-v2`（默认激活）、`strict-v2-v1`（默认激活）、
`lenient-v2-v1`（NEAREST 舍入 + CARRY_ORIGINAL，用于热切换演示）。

---

## 4. 验收命令（双版本客户端）

```bash
make build

# 正常 v1 -> v2（字段重命名 + 单位换算 + 重签 + 溯源）
./bin/demo v1-to-v2

# v2 新枚举 PHASE_MAINTENANCE(4) -> v1：strict 下返回可定位错误，携带 original_enum=4
./bin/demo v2-to-v1 -phase 4

# 整数舍入：亚毫秒/亚毫度输入，strict(REJECT) 报错并指出字段
./bin/demo v2-to-v1 -ts-ns 1727000000123456789 -temp-uk 294400499

# 伪造签名 -> Unauthenticated
./bin/demo v1-to-v2 -bad-signature

# 缺省 vs 显式零（输出 battery_pct_micro 一个缺失、一个为 0）
./bin/demo v1-to-v2 -no-battery
./bin/demo v1-to-v2 -battery-mp 0

# 半流：单条失败不杀流（one outcome per item，保序），流内既有成功也有定位错误
./bin/demo stream-v2-v1 -n 5

# 热切换到 lenient（运行时、不重启）：同一记录从“报错”变为“携带原值 4 / NEAREST 舍入”
./bin/demo activate -name lenient-v2-v1
./bin/demo v2-to-v1 -phase 4                       # 现在 raw_phase=4
./bin/demo v2-to-v1 -ts-ns 1727000000123456789 -temp-uk 294400499
./bin/demo activate -name strict-v2-v1            # 切回 strict

# 用自定义 JSON 映射 upsert + 激活（FLOOR 时间戳、NEAREST 温度、携带枚举）
./bin/demo activate -spec-file examples/mapping_lenient.json
./bin/demo activate -name strict-v2-v1

# 背压：服务端缓冲有界（COMPGW_MAX_IN_FLIGHT），大量消息按序全量到达
./bin/demo backpressure -n 500 -recv-pause 10ms

# 查看当前激活映射
./bin/demo mapping
```

### 查看 PostgreSQL 中的真实落库

```bash
PGPASSWORD=compgw psql -h 127.0.0.1 -U compgw -d compgw \
  -c "SELECT direction, mapping_name, mapping_version, ok, error_code, error_field, record_id, carried_enum
      FROM conversion_audit ORDER BY id DESC LIMIT 10;"

PGPASSWORD=compgw psql -h 127.0.0.1 -U compgw -d compgw \
  -c "SELECT name, direction, version, is_active FROM mappings ORDER BY direction, name;"
```

示例输入（含已签名的 happy path、显式零、缺省、maintenance 枚举、舍入、溢出）：
`./bin/demo gen-examples` 生成在 `examples/*.json`。

---

## 5. 自动化测试

```bash
# 单元测试（无需 DB）：big.Int 换算、负数舍入、溢出、枚举 ERROR/CARRY、HMAC、流引擎
go test ./internal/...

# 集成测试（需要 Postgres，默认连 compgw_test 库；不可用时自动 skip）
go test ./...

# 竞态检测
make test-race
```

集成测试（`test/integration_test.go`，使用 `bufconn` 内存 gRPC + 真实 PostgreSQL）覆盖：

- **未知字段**：追加未知 wire 字段，计数进溯源且往返保留；
- **缺省 vs 显式零**：经过真实 gRPC 栈后 presence 不丢失，二者签名不同；
- **新枚举不可表达**：strict 返回可定位错误（字段/记录/原值），lenient 携带原值；
- **溢出与舍入**：单位换算 `INTEGER_OVERFLOW` / `ROUNDING_REQUIRED`，NEAREST/FLOOR 生效；
- **版本热切换**：激活后 unary 立即生效；**已打开的流钉住开启时的映射版本**，新流用新映射；
- **半流失败**：流中某条失败只返回该条的 per-item 错误，不杀流、保序；
- **取消停止转换**：客户端取消后服务端流在限定时间内结束；
- **背压**：服务端缓冲上限（如 4）下 200 条突发消息按序全量到达，不丢不乱；
- **审计真实入库**：成功/失败/携带枚举都写入 `conversion_audit`。

测试 DSN 可用 `COMPGW_TEST_PG_DSN` 覆盖。

---

## 6. gRPC 接口摘要

```
service CompatGateway {
  rpc ConvertV1ToV2(ConvertV1ToV2Request) returns (ConvertV1ToV2Response);
  rpc ConvertV2ToV1(ConvertV2ToV1Request) returns (ConvertV2ToV1Response);
  rpc ConvertStreamV1ToV2(stream StreamV1Request) returns (stream StreamV2Response);
  rpc ConvertStreamV2ToV1(stream StreamV2Request) returns (stream StreamV1Response);
  rpc GetMapping(GetMappingRequest) returns (GetMappingResponse);
  rpc ActivateMapping(ActivateMappingRequest) returns (ActivateMappingResponse);
}
```

- 每个响应都带 `Provenance{source_version, converted_version, mapping_name,
  mapping_version, unknown_fields_seen, carried_original_enum?}`。
- unary 失败通过 gRPC status code + `ConversionError` detail 定位；流式失败是该条
  消息的 `outcome.error`（保序，不影响其他条目）。
- 流引擎：单 worker 保序，接收协程写入容量为 `max_in_flight` 的有界 channel，缓冲满即
  阻塞并把背压传给客户端流控窗口；`ctx` 取消立即停止，发送失败（半流）取消内部 context 解除接收阻塞。

---

## 7. 设计说明 / 取舍

- **显式映射优于约定**：所有重命名、单位系数、枚举数字表都在 JSON 规格里，可审计、可热切换；
  新增映射不需要改代码。
- **整数不用浮点**：单位换算全部用整数系数 + `big.Int`，避免精度问题；温度的摄氏/开尔文
  偏移用 `pre_add/post_add` 表达。
- **映射钉版**：流在开启时 `PinDirection()` 取得不可变快照，热切换不会让一条流前后混用两套映射。
- **审计不影响主契约**：审计写库失败不拖垮转换 RPC；context 已取消时跳过审计写入。

依赖版本锁定在 `go.mod` / `go.sum`（gRPC v1.66.2、protobuf v1.34.2、pgx v5.7.1 等）。
