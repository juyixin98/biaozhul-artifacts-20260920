# 企业 IT 资产生命周期后端

基于 Spring Boot 3 + MySQL 8 + Docker 的 IT 资产生命周期管理服务。本期范围：**状态转换、折旧计提、可追溯调整**（不含通用库存、采购商城、配置中心）。

## 快速开始

```bash
# 一键启动（MySQL + 应用，含 Flyway 迁移与样例数据）
docker compose up --build

# 本地开发（需先启动 MySQL）
docker compose up mysql -d
mvn spring-boot:run

# 运行测试（Testcontainers 自动拉起真实 MySQL）
mvn test
```

启动后：

- API 文档（Swagger UI）：http://localhost:8080/swagger-ui.html
- OpenAPI JSON：http://localhost:8080/v3/api-docs

演示账号（HTTP Basic）：

| 账号 | 密码 | 角色 | 权限 |
|---|---|---|---|
| `finance` | `finance123` | FINANCE | 计提、关账、成本/年限调整 |
| `manager` | `manager123` | ASSET_MANAGER | 建档、状态转换 |
| `viewer` | `viewer123` | VIEWER | 只读 |

## 领域规则

### 状态机

```
IN_STOCK(库存) → IN_USE(使用中) ⇄ UNDER_REPAIR(维修)
IN_USE / UNDER_REPAIR → RETIRED(退役) → DISPOSED(处置，终态)
```

- 处置为终态，不可恢复使用；非法转换返回 `422`。
- 每次转换携带 `expectedVersion`（乐观并发）与 `requestId`（幂等键）：
  - 重复 `requestId` → 返回首次的变更记录（`replayed: true`），不重复转换；
  - 版本不匹配 → `409`，并发修改只有一个成功；
  - 资产当前状态与仅追加的变更记录（`asset_status_transitions`）在**同一事务**提交。

### 折旧

- 金额一律 `decimal(19,2)` 定点数，计提额按 **HALF_UP 保留 2 位小数**。
- **当月启用、次月计提**；`IN_USE` 与 `UNDER_REPAIR` 状态参与计提。
- **直线法**：每期计提 = round((期初账面 − 残值) / 剩余月数)；最后一个剩余月份计提全部剩余应折旧额，期末恰好等于残值，消除累计舍入误差。
- **双倍余额递减法**：月折旧率 = 2 / 使用年限（月），每期计提 = round(期初账面 × 月折旧率)；剩余最后两个月改按直线法摊销，到期恰好等于残值。
- 任何情况下**账面价值不得低于残值**（计提额 clamp 到期初 − 残值）。
- 计提按 `(asset_id, period)` 数据库唯一约束记账：重跑、重试、并发跑批都**不会重复入账**。
- 整个期间计提单事务提交；期间行与资产行均持悲观写锁，**退役/处置与计提并发时串行化**，结果必居其一，不产生不一致账目。

### 期间关账与参数调整

- 期间关账（`POST /api/periods/{period}/close`）幂等；**已关账期间不可重算/补提**（`409`）。
- 成本/使用年限调整采用**未来适用法**：只影响未关账期间的后续计提，已入账期间一律不回溯。
  - 成本差额记入资产 `pending_book_value_delta`，并入下一期期初（明细中体现为 `adjustment_delta`），入账后清零；
  - 直线法在剩余月份内重新平均；余额递减法按新年限计算折旧率；
  - 每次调整保存**原因与旧参数**（`asset_adjustments`，仅追加，可审计）。

### 勾稽关系（导出可解释）

```
期初(opening) = 上期期末(closing) + 本期前未入账成本调整(adjustment_delta)
期末(closing) = 期初(opening) − 本期计提(amount)
```

`GET /api/depreciation/export?period=YYYY-MM` 导出 CSV，逐行给出上述字段。

## API 一览

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/assets` | ASSET_MANAGER | 资产建档 |
| GET | `/api/assets` / `/api/assets/{id}` | 任意认证用户 | 列表 / 详情 |
| POST | `/api/assets/{id}/transitions` | ASSET_MANAGER | 状态转换（幂等 + 乐观并发） |
| GET | `/api/assets/{id}/transitions` | 任意认证用户 | 状态变更历史 |
| POST | `/api/assets/{id}/adjustments` | FINANCE | 成本/年限调整（未来适用） |
| GET | `/api/assets/{id}/adjustments` | 任意认证用户 | 调整历史（含旧参数与原因） |
| POST | `/api/depreciation/run?period=YYYY-MM` | FINANCE | 执行期间计提（幂等） |
| GET | `/api/depreciation/entries?period=YYYY-MM` | 任意认证用户 | 期间计提明细（JSON） |
| GET | `/api/depreciation/export?period=YYYY-MM` | 任意认证用户 | 期间计提明细（CSV 导出） |
| GET | `/api/periods/{period}` | 任意认证用户 | 期间状态 |
| POST | `/api/periods/{period}/close` | FINANCE | 关账（幂等） |

### 示例

```bash
# 状态转换（库存 -> 使用中）
curl -u manager:manager123 -X POST http://localhost:8080/api/assets/3/transitions \
  -H 'Content-Type: application/json' \
  -d '{"toStatus":"IN_USE","expectedVersion":0,"requestId":"req-0001"}'

# 执行 2026-09 计提
curl -u finance:finance123 -X POST 'http://localhost:8080/api/depreciation/run?period=2026-09'

# 调整成本与年限（保存原因与旧参数）
curl -u finance:finance123 -X POST http://localhost:8080/api/assets/1/adjustments \
  -H 'Content-Type: application/json' \
  -d '{"newCost":14000.00,"newUsefulLifeMonths":48,"reason":"资产改良追加投入"}'

# 关账
curl -u finance:finance123 -X POST http://localhost:8080/api/periods/2026-09/close

# 导出明细
curl -u viewer:viewer123 'http://localhost:8080/api/depreciation/export?period=2026-09' -OJ
```

错误码：`400` 参数/业务校验失败，`401/403` 未认证/越权，`404` 不存在，`409` 版本冲突或期间已关账，`422` 非法状态转换。

## 工程结构

```
src/main/java/com/example/asset/
├── domain/          # Asset、状态机枚举、仅追加的转换/折旧/调整记录、会计期间
├── repository/      # Spring Data JPA，含悲观写锁查询
├── service/         # AssetService(状态机) DepreciationService(计提)
│                    # PeriodService(关账) AdjustmentService(调整) DepreciationCalculator
├── web/             # REST 控制器 + DTO + 全局异常处理
└── config/          # Spring Security（三角色）
src/main/resources/
├── db/migration/    # V1__init.sql 结构迁移
└── db/seed/         # V2__seed.sql 样例数据（仅主配置加载，测试不加载）
```

## 测试

`mvn test` 使用 Testcontainers 拉起真实 MySQL 8.4 执行集成测试，覆盖：

- **重复计提**：同一期间重复跑批不重复记账（`duplicateRunDoesNotDoubleBook`）
- **残值边界**：直线法舍入与期末恰好等于残值、余额递减法全程不低于残值（`straightLineNeverBelowSalvageAndEndsExactly`、`decliningBalanceEndsExactlyAtSalvage`、`DepreciationCalculatorTest`）
- **期间关闭**：关账后重算被拒、关账幂等（`closedPeriodCannotBeRecalculated`）
- **参数调整**：未来适用、旧参数与原因留痕、调整差额只入账一次（`adjustmentAppliesProspectivelyAndKeepsAuditTrail`）
- **并发处置**：并发转换只有一个成功、退役与计提并发不产生不一致账目（`concurrentTransitionsOnlyOneSucceeds`、`concurrentRetireAndDepreciationRunStayConsistent`）
- **幂等转换**：重复 `requestId` 返回首次结果（`duplicateRequestIdIsIdempotent`）
- **权限**：三角色读写边界（`SecurityIT`）
