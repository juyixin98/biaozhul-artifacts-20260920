# 企业 IT 资产生命周期后端

Spring Boot 3.3 + Java 21 + MySQL 8 + Flyway + Docker。本期聚焦三件事：
**状态转换**、**折旧**、**可追溯调整**。不包含通用库存、采购商城或配置中心。

- 金额一律使用定点数 `DECIMAL(18,2)`（元/分），不使用浮点。
- 会计期间用整数 `YYYYMM`（如 `202609`），整月口径计提。
- 状态、政策、分录均带数据库约束，关键并发由“乐观锁 + 悲观行锁 + 唯一索引”三重保证。

## 1. 快速启动

```bash
docker compose up -d --build
# 应用: http://localhost:18087   Swagger: /swagger-ui.html
# MySQL 映射到宿主机 13307（库 itasset / itasset / itassetpass）
```

compose 默认启用 `seed-sample` profile，启动时写入 3 台样例资产并补发 2026 年历史折旧。
本地裸机运行：先起 MySQL（或 `docker compose up -d db`），再 `mvn spring-boot:run`。

内置账号（HTTP Basic，密码在启动时 BCrypt 编码入库）：

| 角色 | 用户名 | 密码 | 权限 |
|---|---|---|---|
| 财务 FINANCE | `finance` | `Finance#2026` | 关账、参数调整、导出 |
| 资产经理 MANAGER | `manager` | `Manager#2026` | 建档、状态转换、计提 |
| 查看者 VIEWER | `viewer` | `Viewer#2026` | 全部 GET 只读 |

样例资产：

| 编号 | 方法 | 成本/残值/月数 | 状态 |
|---|---|---|---|
| NB-2026-001 ThinkPad | 直线 | 12000 / 600 / 36 | 维修中（已计提 1–8 月）|
| SRV-2026-002 服务器 | 余额递减 | 60000 / 3000 / 48 | 使用中（已计提 3–8 月）|
| PR-2026-003 打印机 | 直线 | 3000 / 150 / 36 | 库存（未启用，无折旧）|

## 2. 数据模型与迁移

Flyway 迁移位于 `src/main/resources/db/migration/`，应用启动自动执行：

- `V1__init_schema.sql` —— 6 张表（见下）。
- 内置用户不写死哈希，改由 `DefaultUsersSeeder` 启动时幂等写入，避免迁移脚本里固化不可校验的 BCrypt 串。

| 表 | 关键约束 |
|---|---|
| `asset` | `asset_code` 唯一；`@Version` 乐观锁；`CHECK(0 ≤ salvage ≤ cost)`、`life > 0` |
| `status_transition` | **仅追加**；`request_id` 全局唯一（幂等键）|
| `depreciation_policy` | `(asset_id, sequence_no)` 唯一；政策链只追加不改写 |
| `depreciation_entry` | **`(asset_id, period)` 唯一** —— 重跑不可能重复记账；`charge ≥ 0` |
| `accounting_period` | 期间仅在关账时落一行（存在即已关账）|
| `app_user` | 用户名唯一 |

## 3. 状态机

```
IN_STOCK 库存      ──> IN_USE 使用中
IN_USE   使用中    ──> IN_REPAIR 维修 | RETIRED 退役
IN_REPAIR 维修     ──> IN_USE | RETIRED
RETIRED  退役      ──> DISPOSED 处置
DISPOSED 处置      ──> （终态，任何转出都返回 422 ILLEGAL_TRANSITION）
```

- `库存 → 使用中` 时自动写入启用日期（当月整月开始计提）并建立第 1 条折旧政策。
- 退役/处置**不改账、不删分录**；之后不再对该资产计提。
- 转换请求体必须带 `expectedVersion` 与 `requestId`：
  - 版本不符 → `409 VERSION_MISMATCH`（或提交时乐观锁失败 `409 VERSION_CONFLICT`），**并发只有一个成功**；
  - 相同 `requestId` → 返回首次结果，不产生第二条流水（幂等）；
  - 资产当前状态与流水在**同一事务**提交。

## 4. 折旧规则

| 规则 | 口径 |
|---|---|
| 精度 | 中间除法保留 6 位小数，落账 `HALF_UP` 保留 2 位（四舍五入）|
| 启用 | 启用当月计提一整月；库存/退役/处置状态不计提（维修中照常计提）|
| 直线法 | `月计提 =（期初账面净值 − 残值）/ 剩余月数` |
| 余额递减 | 双倍余额递减：`月率 = 2 / 原始预计使用月数`，`月计提 = 期初净值 × 月率` |
| 残值下限 | 任何月份期末净值 **≥ 残值**；非末期按率计算会跌破时夹到 `期初−残值` |
| 末期补差 | 政策窗口最后一个月 `计提 = 期初净值 − 残值`，精确落到残值 |
| 顺序性 | 必须逐月计提，跳月返回 `422 MISSING_PRIOR_PERIOD` |
| 唯一性 | `(资产,期间)` 数据库唯一；重复调用返回原分录（`ALREADY_POSTED`）|

计提按期间对全部可计提资产批量执行，每个资产独立事务：
单台资产被拒（如已关账、超年限、状态不可计提）不影响其它资产，结果逐台返回
`POSTED / ALREADY_POSTED / FULLY_DEPRECIATED / REJECTED`。

## 5. 可追溯调整

成本、残值、使用年限、折旧方法均可调整（仅财务），采用**未来适用法**：

1. `effectivePeriod` 必须是合法未关账期间，且严格晚于现有最新政策生效期间；
2. 生效期间及以后不得已有分录（否则 `PERIOD_ALREADY_POSTED`）——
   **调整只能影响未关账期间，历史账永不重算**；
3. 追加一条新政策（`sequence_no+1`），记录**旧政策原样保留**、新参数、
   生效期初账面净值、调整原因 `reason` 与操作人；
4. 新残值不得高于生效期初净值。

**退役/处置与计提并发**：计提在事务内先对资产行加 `SELECT … FOR UPDATE`
悲观锁，再复查状态后写分录，因此两种完成顺序下账目都自洽
（先处置则当月不记账；先记账则该分录合法且之后不再新增）。锁冲突/死锁统一返回
`409 CONCURRENT_MODIFICATION`，配合 requestId 可安全重试。

## 6. API 一览

交互式文档：`GET /swagger-ui.html`、`GET /v3/api-docs`。
除文档页外全部需要 HTTP Basic。

### 资产（`/api/assets`）

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /api/assets` | MANAGER | 建档；可带 `placedInService` 直接启用 |
| `GET /api/assets` `GET /api/assets/{id}` | 只读 | 列表/详情（含当前版本）|
| `POST /api/assets/{id}/transitions/{target}` | MANAGER | 状态转换，body 见下 |
| `GET /api/assets/{id}/transitions` | 只读 | 仅追加的变更流水 |
| `POST /api/assets/{id}/adjustments` | FINANCE | 折旧参数调整（追加政策）|
| `GET /api/assets/{id}/depreciation/policies` | FINANCE/MANAGER | 政策链（旧参数、原因、操作人）|
| `GET /api/assets/{id}/depreciation/entries` | 只读 | 逐期 期初/计提/期末 |
| `POST /api/assets/depreciation/post` | MANAGER | 按期间批量计提 `{"period":202609}` |

### 期间与导出

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /api/periods/close` | FINANCE | 关账（重复幂等）|
| `GET /api/periods` `GET /api/periods/{period}` | 只读 | 关账状态 |
| `GET /api/export/depreciation/{period}` | 只读 | 期内全部资产明细 CSV（UTF-8 BOM，含合计行，可解释期初/计提/期末与公式）|
| `GET /api/export/assets/{id}/depreciation` | 只读 | 单资产全期 JSON |

请求示例：

```bash
# 建档（库存）
curl -u manager:Manager#2026 -H 'Content-Type: application/json' -X POST http://localhost:18087/api/assets -d '{
  "assetCode":"NB-2026-009","name":"笔记本","department":"研发部",
  "purchaseCost":12000.00,"salvageValue":600.00,
  "usefulLifeMonths":36,"depreciationMethod":"STRAIGHT_LINE"}'

# 转换（expectedVersion 取资产当前 version；requestId 由客户端生成并保持复用）
curl -u manager:Manager#2026 -H 'Content-Type: application/json' \
  -X POST http://localhost:18087/api/assets/4/transitions/IN_USE \
  -d '{"requestId":"8c1f…-uuid","expectedVersion":0,"note":"领用"}'

# 计提 / 关账 / 调整 / 导出
curl -u manager:Manager#2026 -d '{"period":202609}' -H 'Content-Type: application/json' \
  -X POST http://localhost:18087/api/assets/depreciation/post
curl -u finance:Finance#2026 -d '{"period":202609}' -H 'Content-Type: application/json' \
  -X POST http://localhost:18087/api/periods/close
curl -u finance:Finance#2026 -H 'Content-Type: application/json' \
  -X POST http://localhost:18087/api/assets/4/adjustments \
  -d '{"effectivePeriod":202610,"reason":"延寿评估","usefulLifeMonths":36}'
curl -u finance:Finance#2026 http://localhost:18087/api/export/depreciation/202609 -o sep.csv
```

错误响应统一为 `{"timestamp","status","code","message"}`，
`code` 取值：`ILLEGAL_TRANSITION / VERSION_MISMATCH / VERSION_CONFLICT /
REQUEST_ID_CONFLICT / CONCURRENT_MODIFICATION / PERIOD_CLOSED /
PERIOD_ALREADY_POSTED / MISSING_PRIOR_PERIOD / INVALID_PERIOD /
SALVAGE_TOO_HIGH / NO_POLICY / ASSET_NOT_DEPRECIABLE / VALIDATION_FAILED /
FORBIDDEN / NOT_FOUND`。

## 7. 测试（真实 MySQL）

测试全部在**真实 MySQL 8.4** 上运行（非 H2/模拟），覆盖需求点名的场景：

- `DepreciationCalculatorTest` —— 舍入、残值下限、末期补差（纯单元）。
- `DepreciationPostingIntegrationTest` —— **重复计提幂等**、**残值边界**、
  直线 12 个月精确到残值、余额递减全寿命不低于残值、跳月拒绝、非法期间拒绝。
- `PeriodCloseIntegrationTest` —— **已关账期间不可计提/调整**、关账幂等、
  已计提（未关账）期间同样不可调整。
- `AdjustmentIntegrationTest` —— **参数调整**只影响未来、旧参数与原因保留、
  方法切换后的计算。
- `AssetTransitionIntegrationTest` —— 状态机、处置终态、**requestId 幂等**、
  旧版本拒绝、**同版本并发只有一个成功**、状态与流水同事务。
- `DisposalConcurrencyIntegrationTest` —— **处置与同期间计提并发 6 轮**，
  断言两种完成顺序下账目都自洽。
- `ApiSecurityIntegrationTest` —— HTTP Basic 与三角色权限矩阵、关账/导出全链路。

两种运行方式：

```bash
# 方式一：本机 Docker 可用时，Testcontainers 自动起一次性 MySQL（推荐）
mvn test

# 方式二：无 docker socket 的环境，指向外部 MySQL
docker run -d --name itasset-test-mysql -p 13398:3306 \
  -e MYSQL_DATABASE=itasset_test -e MYSQL_USER=itest -e MYSQL_PASSWORD=itestpass \
  -e MYSQL_ROOT_PASSWORD=rootpass mysql:8.4
IT_MYSQL_URL='jdbc:mysql://127.0.0.1:13398/itasset_test?useSSL=false&allowPublicKeyRetrieval=true' \
IT_MYSQL_USER=itest IT_MYSQL_PASSWORD=itestpass mvn test
```

## 8. 本期范围之外

通用出入库/库存台账、采购商城、组织/配置中心、折旧凭证推送总账、
期间重开（reopen）等均不在本期；`accounting_period` 一旦关账即视为终态。
