# 企业 IT 资产生命周期后端

Spring Boot 3.3 + MySQL 8.4 + Docker 实现的 IT 硬件资产财务台账后端。
本期范围**仅**包含：资产状态转换、月度折旧（直线法/余额递减法）、可追溯参数调整、
会计期间关账与明细导出。**不包含**通用库存、采购商城、配置中心。

## 1. 快速开始

```bash
docker compose up -d --build
# 应用: http://localhost:18088   MySQL: localhost:3307
```

首次启动 Flyway 自动执行：
- `db/migration/V1__init_schema.sql`：建表、约束、仅追加触发器
- `db/migration/V2__sample_data.sql`：5 个样例资产与手工折旧分录

本地裸机运行：`mvn spring-boot:run`（需自行准备 MySQL，可用 `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` 覆盖连接）。

真实数据库测试（Testcontainers 自动拉起 MySQL 8.4）：

```bash
mvn test        # 26 个测试，含真实 MySQL 行锁/唯一约束/触发器验证
```

## 2. 角色与认证

HTTP Basic（演示用，生产应换 OAuth2/JWT）：

| 角色 | 账号 | 权限 |
|---|---|---|
| `FINANCE` | finance / finance123 | 期间关账、折旧参数调整、触发折旧、只读 |
| `ASSET_MANAGER` | manager / manager123 | 资产建档、状态转换、触发折旧、只读 |
| `VIEWER` | viewer / viewer123 | 只读 |

## 3. 数据模型与金额规则

- 所有金额定点数：`DECIMAL(18,4)` 存储；月度计提额按人民币分 **HALF_UP（四舍五入）保留 2 位**。
- 资产字段：采购成本、残值、启用日期、预计使用月数、部门、折旧方法、余额递减年率。
- 状态：`IN_STOCK 库存 → IN_USE 使用中 → UNDER_REPAIR 维修 / RETIRED 退役 / DISPOSED 处置`。

仅追加台账表（`status_change`、`depreciation_entry`、`parameter_adjustment`、`depreciation_run`）
由 **MySQL 触发器在数据库层禁止任何 UPDATE/DELETE**（应用被绕过也无法篡改历史）。

## 4. 状态机

| 当前状态 | 允许的目标状态 |
|---|---|
| IN_STOCK | IN_USE, RETIRED, DISPOSED |
| IN_USE | IN_STOCK, UNDER_REPAIR, RETIRED, DISPOSED |
| UNDER_REPAIR | IN_USE, RETIRED, DISPOSED |
| RETIRED | DISPOSED |
| DISPOSED | （终态，**不可恢复使用**） |

转换请求携带 `expectedVersion`（JPA `@Version` 乐观锁）与 `requestId`（幂等键）：

- 同 `requestId` 重试 → 返回首次转换记录，状态与版本不重复推进；
- 版本不符、非法转换 → `409 Conflict`；
- 并发转换：资产行 `SELECT … FOR UPDATE` 串行化，**只有一个成功**；
- 资产当前状态更新与变更记录插入**同一事务提交**。

## 5. 折旧规则

- **计提惯例**：当月增加次月起计提；退役/处置**当月仍计提，次月起拒绝计提**。
  （状态转换按提交当月生效，不支持回溯日期。）
- 期间格式 `yyyyMM`；必须连续计提，不允许跳期、补提未来期间。
- **直线法**：月折旧 = (成本 − 残值) / 使用月数；逐月四舍五入到分，
  **段末月补差**（期初账面 − 残值），期末精确等于残值。
- **余额递减法**：月折旧率 = 年率 / 12，月折旧 = 期初账面 × 月率；
  同样四舍五入到分，使用年限末月补差到残值。
- **残值地板**：任何月份期末账面价值不得低于残值；触达残值后计提 0。
- **唯一性/幂等**：`unique(asset_id, period)` 保证任务按资产和期间唯一，
  重跑只跳过不重复记账；`depreciation_run.request_id` 唯一保证请求重放幂等。
- **参数调整（未来适用法）**：调整只能从"下一个尚未计提的期间"生效，
  已生成的仅追加分录（尤其已关账期间）永不改写；调整开启新折旧段，
  以调整时点账面价值为基数，在（新使用月数 − 已计提月数）内摊销。
  调整记录保存原因、全部旧参数、新参数、调整时点账面值与段月数。

### 并发一致性

- 同一资产上的转换/计提/调整：先持有资产行锁，严格串行。
- 同一期间上的计提 vs 关账/调整：`period_mutex` 表 `INSERT IGNORE` + `FOR UPDATE`
  串行化（不同表之间无法仅靠间隙锁互斥，故引入显式期间互斥量）。
- 锁序固定"资产锁 → 期间锁"，避免死锁。
- 已关账期间（含其更早期间）永久冻结；关账按月顺序进行，不提供反关账
  （差错通过未来适用法调整并留痕）。

## 6. API 说明

所有写接口均要求 `requestId`（调用方生成的 UUID），重复请求幂等。

### 资产管理（ASSET_MANAGER 写）

```
POST   /api/assets                      建档
GET    /api/assets                      列表（三角色）
GET    /api/assets/{id}                 详情
```

```json
POST /api/assets
{
  "assetCode":"NB-2026-010","name":"ThinkPad X1",
  "purchaseCost":12000.0000,"salvageValue":1200.0000,
  "inServiceDate":"2026-01-15","usefulLifeMonths":36,
  "department":"财务部","depreciationMethod":"STRAIGHT_LINE",
  "decliningRatePct": null
}
```

校验：残值 0 ≤ 残值 ≤ 成本；使用月数 1~600；余额递减法年率 ∈ (0,100)。

### 状态转换（ASSET_MANAGER）

```
POST /api/assets/{id}/transitions
GET  /api/assets/{id}/transitions       转换历史（仅追加）
```

```json
{"targetStatus":"IN_USE","expectedVersion":0,"requestId":"uuid","reason":"发放领用"}
```

### 折旧（ASSET_MANAGER / FINANCE）

```
POST /api/assets/{id}/depreciation/runs     计提区间（按月连续）
GET  /api/assets/{id}/depreciation/entries  分录列表（?fromPeriod&toPeriod）
```

```json
{"fromPeriod":"202602","toPeriod":"202603","requestId":"uuid"}
```

响应区分 `createdEntries`（新记账）与 `skippedPeriods`（已存在，跳过）。

### 参数调整（FINANCE）

```
POST /api/assets/{id}/adjustments      字段缺省即沿用现值
GET  /api/assets/{id}/adjustments      调整留痕列表
```

```json
{"effectivePeriod":"202604","newUsefulLifeMonths":48,
 "newSalvageValue":1000.0000,"reason":"实际使用强度低于预期","requestId":"uuid"}
```

### 期间关账（FINANCE）

```
POST /api/periods/{period}/close       例: /api/periods/202602/close
GET  /api/periods/closes               已关账列表；?period=202602 返回 closed 标记
```

### 导出（三角色）

```
GET /api/exports/periods/{period}                 JSON（?assetId 过滤）
GET /api/exports/periods/{period}?format=csv     CSV（带 UTF-8 BOM，Excel 友好）
```

每行给出 `openingBookValue 期初 - depreciationAmount 计提 = closingBookValue 期末`，
并附折旧方法、段内月序号、参数快照与中文解释文字，可直接用于财务复核。

### 错误约定

`400` 参数/业务规则错误；`409` 版本冲突、非法转换、期间不连续、期间已关账、
退役后期间计提等；`403` 角色不足；`404` 资产不存在。

## 7. 测试覆盖（真实 MySQL 8.4，Testcontainers）

| 场景 | 测试类 |
|---|---|
| 重复/并发计提不重复记账 | `ConcurrencyIntegrationTest`、`DepreciationIntegrationTest` |
| 残值边界、末月补差、账面不破残值 | `DepreciationCalculatorTest`、`DepreciationIntegrationTest` |
| 期间关闭后不可计提/重算、顺序关账 | `PeriodCloseIntegrationTest` |
| 参数调整只影响未关账期间、旧参数留痕 | `ParameterAdjustmentIntegrationTest` |
| 并发处置与计提串行、处置不可恢复 | `ConcurrencyIntegrationTest`、`StatusTransitionIntegrationTest` |
| 乐观锁/requestId 幂等/并发仅一个成功 | `StatusTransitionIntegrationTest` |
| 仅追加触发器（绕过应用直改库被拒） | `AppendOnlyTriggerIntegrationTest` |
| 三角色 HTTP 权限 | `SecurityRolesIntegrationTest` |

## 8. 工程结构

```
src/main/java/com/itasset/
├── config/      SecurityConfig(HTTP Basic 三角色), UserContextFilter
├── domain/      Asset / StatusChange / DepreciationEntry /
│                ParameterAdjustment / PeriodClose / PeriodMutex / DepreciationRun
├── repo/        JPA 仓储（FOR UPDATE 行锁）
├── service/     状态机、定点折旧计算器、关账、调整、导出、会计期间工具
└── web/         REST 控制器、DTO、全局异常处理
src/main/resources/db/migration  Flyway 建表 + 触发器 + 样例
```
