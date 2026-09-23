# 协议版本兼容网关 (Protocol-Version Compatibility Gateway)

纯后端本地 gRPC 网关,在两个遥测 schema 版本(`telemetry/v1` ↔ `telemetry/v2`)之间做显式、可校验的双向转换,并把每条转换记录审计到 PostgreSQL。技术栈:Go 1.22、gRPC、PostgreSQL(pgx/v5)。

## 两个 schema 的差异(显式映射)

| v1 (`telemetry.v1.Reading`) | v2 (`telemetry.v2.Reading`) | 转换 |
|---|---|---|
| `device_id` | `device_uid` | 字段重命名 |
| `temp_celsius_e2` (int32, 0.01 °C, optional) | `temperature_millikelvin` (int64, mK, optional) | 单位变换 `mK = cC*10 + 2 731 500`;反向检查**舍入**(余数≠0)与 **int32 溢出** |
| `condition` (4 值枚举) | `condition` (6 值,新增 `MAINTENANCE=4`、`CALIBRATING=5`) | 显式枚举映射表 |
| `observed_at_unix_ms` (int64) | `observed_at` (google.protobuf.Timestamp) | 类型变换,亚毫秒精度按舍入策略处理 |
| — | `firmware_rev` | v2 独有,v2→v1 丢弃并在 Meta 中报告 |
| `seq_no` | `seq_no` | 恒等 |

## 关键语义

- **不静默降级**:v2 独有枚举在 `strict` 映射下返回**可定位错误**(`ConversionError{field_path, reason, raw_value}`,随流内 Envelope 返回,原始值 verbatim 保留);在 `carry` 映射下输出 `CONDITION_UNSPECIFIED` 并在 `Meta.attributes` 携带 `unmapped_condition_raw`/`unmapped_condition_name`。两种策略都不会把未知枚举静默写成 0。
- **整数换算检查**:mK→cC 的 `/10` 余数非零时,`strict` 报错、`half_even` 银行家舍入并记录 `rounded=true` 与余数;结果超出 int32 一律报溢出错误。
- **缺省 vs 显式零**:温度字段为 `optional`(presence 跟踪)。显式 `0` cC → 显式 `2731500` mK;未设置 → 未设置。
- **溯源**:每条输出 Envelope 的 `Meta` 携带 `source_version`、`target_version`、`converter_version`、`mapping_version`、`record_id`(payload SHA-256 前 16 hex)。
- **背压**:每流一条容量有界的 channel(默认 32,`-buffer` 可调)。下游不读 → Send 阻塞 → 缓冲填满 → 接收循环停止拉取,由 gRPC 流控把压力回传给客户端。
- **取消即停**:流 context 取消后,接收与转换两侧都立即退出;`in_flight` 归零。
- **版本热切换**:`ReloadMapping` RPC(或 `gwctl reload <version>`)在运行时原子切换映射;下一条记录即生效。空版本号重载 `mappings.json`。
- **审计**:每条记录(ok 或 error)写入 `conversion_audit`,含方向、双方版本、转换器版本、映射版本、错误明细(JSONB)与 payload SHA-256。

## 目录

```
proto/            # v1/v2 telemetry + gateway 服务定义
gen/              # protoc 生成代码(已提交)
internal/convert  # 显式映射、单位/枚举/时间转换、可定位错误
internal/registry # 映射注册表,原子热切换
internal/server   # 流式服务:有界缓冲背压、取消、审计
internal/store    # PostgreSQL 审计(pgx/v5)
cmd/gateway       # 网关服务
cmd/clientv1      # v1(旧版)客户端
cmd/clientv2      # v2(新版)客户端
cmd/gwctl         # 管理 CLI:热切换映射、查看统计
examples/         # 示例输入(JSONL)
migrations/       # 审计表 DDL
scripts/acceptance.sh  # 端到端验收脚本
```

## 本地启动

前置:Go ≥ 1.22、PostgreSQL ≥ 14(本地或 `make db-up` 用 Docker 起 `postgres:16-alpine`)。

```bash
# 1. 准备数据库(本地已运行的 Postgres)
sudo -u postgres psql -c "CREATE USER telegw WITH PASSWORD 'telegw';" \
                     -c "CREATE DATABASE telegw OWNER telegw;"
# 或用 Docker: make db-up

# 2. 构建
make build

# 3. 启动网关(自动执行 migrations/0001_init.sql)
export GATEWAY_DATABASE_DSN="postgres://telegw:telegw@localhost:5432/telegw"
./bin/gateway -addr 127.0.0.1:50051 -mapping strict-v1
# 也可以: make run
```

不设 `GATEWAY_DATABASE_DSN` 也能启动,但审计持久化会禁用(日志有警告)。

## 验收命令

```bash
# 单元 + 进程内流式测试(无需数据库)
make test

# PostgreSQL 集成测试
make test-integration        # 或 GATEWAY_TEST_DSN=... go test ./internal/store/ -v

# 端到端验收(自动起网关、跑双客户端、热切换、校验审计表)
make acceptance
```

手动验收:

```bash
# v1 客户端:v1 输入 -> v2 输出(观察显式 0 -> 2731500 mK、溯源 Meta)
./bin/clientv1 -addr 127.0.0.1:50051 -input examples/readings_v1.jsonl

# v2 客户端,strict 映射:MAINTENANCE/CALIBRATING 产生可定位错误 Envelope
./bin/clientv2 -addr 127.0.0.1:50051 -input examples/readings_v2.jsonl

# 热切换到 carry 映射后重跑:枚举原值随 attributes 携带,不再报错
./bin/gwctl -addr 127.0.0.1:50051 reload carry-v1
./bin/clientv2 -addr 127.0.0.1:50051 -input examples/readings_v2.jsonl

# 统计与审计
./bin/gwctl -addr 127.0.0.1:50051 stats
psql "$GATEWAY_DATABASE_DSN" -c \
  "SELECT direction, mapping_version, status, count(*) FROM conversion_audit GROUP BY 1,2,3;"
```

## 测试覆盖(对应需求)

| 需求 | 测试 |
|---|---|
| 未知字段 | `TestUnknownFieldsCountedAndReported`(protowire 注入未知字段,计数并记入 attributes) |
| 缺省 vs 显式零 | `TestPresenceExplicitZeroVsUnset` |
| 版本热切换 | `TestMappingHotSwapMidStream`(流进行中 ReloadMapping,行为即时改变)、`registry` 包测试 |
| 半流失败 | `TestClientCancelStopsConversion`(客户端中途取消,服务端停止转换、in_flight 归零) |
| 背压 | `TestBackpressureBoundsBuffer`(下游阻塞时,缓冲=1 的服务端不多读) |
| 枚举扩展 | strict 可定位错误 / carry 携带原值,两个方向 |
| 溢出与舍入 | `TestOverflowChecked`、`TestRoundingStrictRejectsInexact`、`TestRoundingHalfEven` |
| 审计持久化 | `internal/store` 集成测试(真实 PostgreSQL) |

## 重新生成 protobuf 代码

```bash
make proto   # 需要 protoc、protoc-gen-go、protoc-gen-go-grpc
```

依赖通过 `go.mod`/`go.sum` 锁定(grpc v1.69.4、protobuf v1.36.1、pgx/v5 v5.7.2,兼容 Go 1.22)。
