# IT 资产生命周期后端 (IT Asset Lifecycle Backend)

企业 IT 硬件资产的**状态转换、折旧、可追溯调整**后端。使用 Spring Boot 3、MySQL 8、Flyway、
Spring Security（HTTP Basic）、Testcontainers（真实 MySQL 集成测试）。

> 本期范围只含：状态机转换、直线/余额递减折旧、期间关账、会计调整、可追溯审计、明细导出。
> 不含：通用库存、采购商城、配置中心。

---

## 1. 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build -d
# MySQL 映射宿主端口默认 33060（避免占用 3306），应用默认 8080：
APP_HOST_PORT=8080 MYSQL_HOST_PORT=33060 docker compose up --build -d
```

应用启动时 Flyway 自动建表并写入样例数据。健康检查：`GET /actuator/health`。

### 本地运行

```bash
# 1) 准备数据库
mysql -uroot -p -e "CREATE DATABASE itasset; \
  CREATE USER 'itasset'@'%' IDENTIFIED BY 'itasset_pw'; \
  GRANT ALL ON itasset.* TO 'itasset'@'%';"
# 2) 启动
mvn spring-boot:run
# 可通过环境变量覆盖：MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD
```

### 测试（真实 MySQL）

```bash
mvn test          # 使用 Testcontainers 启动一次性 MySQL 8.4，无需本地数据库
```

22 个端到端测试覆盖：重复计提、残值边界、期间关闭、参数调整、并发处置、幂等、导出与解释。

### 内置账号（密码均为 `Pass#2026`）

| 用户名   | 角色          | 权限                                   |
|----------|---------------|----------------------------------------|
| finance  | FINANCE       | 关账、折旧计提、会计调整；全部只读     |
| manager  | ASSET_MANAGER | 维护资产与状态转换；全部只读           |
| viewer   | VIEWER        | 只读                                   |

所有写接口用 HTTP Basic 鉴权。

---

## 2. 资产状态机

状态：`IN_STOCK`（库存）、`IN_USE`（使用中）、`UNDER_REPAIR`（维修）、`RETIRED`（退役）、
`DISPOSED`（处置）。

允许的转换（白名单，服务端强校验）：

```
IN_STOCK      -> IN_USE          （启用，确定启用日期/使用年限/折旧参数）
IN_STOCK      -> DISPOSED       （未使用直接报废）
IN_USE        -> UNDER_REPAIR
IN_USE        -> RETIRED
UNDER_REPAIR  -> IN_USE          （修复回用）
UNDER_REPAIR  -> RETIRED
RETIRED       -> DISPOSED
DISPOSED      -> （终态，不可恢复，任何转出都被拒绝）
```

- 退役后不可再投入使用；处置后全局不可逆。
- 每次转换请求携带 **`expectedVersion`**（读到的资产版本）与 **`X-Request-Id`**。
  - 版本不匹配返回 **409**，并发修改只有一个成功。
  - 相同 `X-Request-Id` + 相同请求体：幂等，返回首次结果，不重复记账。
  - 相同 `X-Request-Id` + 不同请求体：返回 **409**。
- **当前状态更新与仅追加变更记录（`asset_transition`）在同一事务提交**。
  转换先对资产行取写锁（`SELECT … FOR UPDATE`）再插入子记录，保证并发转换不产生
  FK 死锁；竞争失败者随后在版本校验处得到 409。

---

## 3. 金额与期间约定

- 所有金额为**定点数** `DECIMAL(18,2)`，绝不使用浮点存储；JSON 序列化为两位小数。
- 期间为日历月，字符串 `YYYY-MM`（字典序即时间序）。
- 启用月不计提，次月起计提（**当月增加，次月计提**）；退役/处置当月照常计提，
  次月起停止（**当月减少，当月照提**）。

### 直线法（SL）

- 月折旧额 = `(原值 − 残值) / 使用月数`，`HALF_UP` 四舍五入到分，在启用/调整时固定为
  `sl_monthly`，因此每个常规期间金额一致。
- 最后一个期间只计提“账面价值 − 残值”的差额（兜底），**账面价值永远不低于残值**，
  累计舍入误差被末期吸收。

### 双倍余额递减法（DDB）

- 月折旧率 = `2 / 使用月数`（年率 `2/使用年数` 平摊到 12 个月），保留 8 位小数，固定在
  `ddb_rate`。例：48 个月 → 月率 `0.04166667`。
- 月折旧额 = `期初净值 × 月率`，`HALF_UP`。每期受残值下限钳制：
  `期末净值 ≥ 残值`；折旧到残值后不再下降。不切换为直线法。

### 计提任务（月度结账运行）

- 每次计提按“资产 + 期间”生成唯一明细（`depreciation_entry` 的 `(asset_id, period)`
  唯一键是“重跑不重复记账”的硬保证）。
- 一次运行可补齐从 `lastPostedPeriod+1` 到目标期间的所有漏提月份。
- 同一期间重复运行（任何 `X-Request-Id`）都不会再生成已存在的明细；
  `X-Request-Id` 唯一约束保证请求幂等。
- 调整后可对已运行期间做“补提”（新增运行行，明细仍以资产+期间去重）。

---

## 4. 关账与调整

### 关账（FINANCE）

- `POST /api/periods/close`：关账必须**按月连续、无跳月**，且不能关未来月。
- 期间 `P` 一旦关账，`P` 及其之前所有期间锁定：
  - 不可对其计提折旧（409）；
  - 调整的生效期间不得落入关账区间（409）。
- 计提、关账、调整共用同一把 MySQL 命名锁（`GET_LOCK('itasset_month_end')`），
  配合资产行锁，保证“退役/处置”与“计提”并发时账目一致。

### 会计调整（FINANCE）

仅可调整**未关账期间**，保存调整原因和全部旧参数：

- 入参：新原值、新残值、新使用月数、新折旧方法、生效期间、原因。
- 生效期间不得早于首个应提期间，不得晚于“下一个未提月份”（不留空挡）。
- 生效期间及之后**已记账的未关账明细被冲回**；之前期间与已关账明细不动。
- 新参数自“冲回后的账面净值”起**未来适用**；旧原值/残值/年限/方法、新参数、
  原因、冲回条数全部追加写入 `asset_adjustment`（仅追加，可审计）。
- 校验：新残值不得高于新账面净值；新原值不得低于账面净值（不得隐形重估/追溯）。

### 退役/处置 与 计提并发

- 计提运行持有月度命名锁并对资产行加锁；退役/处置在锁上排队。
- 计提会更新净值（推进行版本号）：若运行先提交，进行中的退役会收到 409，
  客户端重读版本后重试即可；账目不会出现“退出期之后的计提”或净值错配。

---

## 5. HTTP API

所有写接口都要求请求头 `X-Request-Id: <客户端生成的唯一值>`。请求/响应均为 JSON。

### 资产（读：全部角色；建/改：ASSET_MANAGER，建也允许 FINANCE）

| 方法 & 路径 | 说明 |
|---|---|
| `GET /api/assets?department=&page=&size=` | 分页列表 |
| `GET /api/assets/{id}` | 资产详情（含版本、净值、累计折旧） |
| `POST /api/assets` | 新建（库存态），需 `X-Request-Id` |
| `POST /api/assets/{id}/transitions` | 白名单状态转换 |
| `GET /api/assets/{id}/transitions` | 仅追加的转换历史 |

创建资产请求体：

```json
{ "assetCode": "A007", "name": "ThinkPad X1", "department": "IT",
  "cost": 12000.00, "salvageValue": 1200.00, "method": "SL" }
```

转换请求体：

```json
{ "targetStatus": "IN_USE", "expectedVersion": 0,
  "effectiveDate": "2026-02-10", "usefulLifeMonths": 36, "reason": "发放给张三" }
```

退役/处置可只传：

```json
{ "targetStatus": "RETIRED", "expectedVersion": 3,
  "effectivePeriod": "2027-02", "reason": "到期退役" }
```

### 折旧 / 关账 / 调整（FINANCE）

| 方法 & 路径 | 说明 |
|---|---|
| `POST /api/depreciation/runs` | 对某期间计提（`{"period":"2026-02"}`） |
| `POST /api/periods/close` | 关账（`{"period":"2026-02", "note":"月结"}`） |
| `GET  /api/periods` | 已关账期间列表 |
| `POST /api/assets/{id}/adjustments` | 会计调整 |
| `GET  /api/assets/{id}/adjustments` | 调整历史（旧参数+原因） |

调整请求体：

```json
{ "newCost": 12000.00, "newSalvageValue": 900.00,
  "newUsefulLifeMonths": 24, "newMethod": "SL",
  "effectivePeriod": "2026-05", "reason": "评估后修订剩余使用年限与残值" }
```

### 报表（全部角色只读）

| 方法 & 路径 | 说明 |
|---|---|
| `GET /api/depreciation/entries?period=YYYY-MM` | 某期间全部已记账明细 |
| `GET /api/assets/{id}/depreciation?throughPeriod=YYYY-MM` | 逐期**期初/计提/期末**解释（含已记账与未来预测） |
| `GET /api/export/depreciation?period=YYYY-MM` | 导出该期间计算明细 CSV（含 UTF-8 BOM，Excel 可直接打开） |

错误响应统一为：

```json
{ "timestamp": "...", "status": 409, "error": "Conflict",
  "message": "Version mismatch: expected 2 but current version is 3", "path": "..." }
```

典型状态码：`400` 参数缺失/非法、`401` 未认证、`403` 角色不足、`404` 资产不存在、
`409` 版本冲突/幂等载荷不一致/期间已关账、`422` 业务规则不允许（非法转换、跳月关账、
未来期间、调整超界等）。

---

## 6. 数据模型（Flyway 迁移）

- `V1__core_schema.sql`：全部表结构（金额 DECIMAL、唯一约束、CHECK 约束、外键、索引）。
- `V2__seed_data.sql`：3 个账号 + 5 台样例资产（含已记账到 2025-12 的 SL/DDB 明细）+
  已关账期间 2025-12。

核心表：

| 表 | 作用 |
|---|---|
| `app_user` | 账号、BCrypt 密码、角色 |
| `hardware_asset` | 资产主数据、当前状态、净值、`version` 乐观锁 |
| `asset_transition` | 仅追加状态转换记录（含预期/结果版本、请求 id） |
| `depreciation_entry` | 资产+期间唯一的月度折旧明细（期初/计提/期末） |
| `depreciation_run` | 月度计提运行（按请求 id 幂等） |
| `asset_adjustment` | 仅追加的会计调整（全部旧/新参数+原因） |
| `period_close` | 关账标记（连续关账边界） |
| `idempotent_request` | 写请求幂等存储（请求 id + 载荷指纹 + 首次响应） |

JPA `ddl-auto=validate`：表结构完全由 Flyway 迁移拥有，Hibernate 只做校验。

---

## 7. 示例调用

```bash
# 新建
curl -u manager:Pass#2026 -H 'Content-Type: application/json' -H 'X-Request-Id: demo-1' \
  -d '{"assetCode":"A007","name":"ThinkPad X1","department":"IT","cost":12000,"salvageValue":1200,"method":"SL"}' \
  http://localhost:8080/api/assets

# 启用（拿到 id=7，版本 0）
curl -u manager:Pass#2026 -H 'Content-Type: application/json' -H 'X-Request-Id: demo-2' \
  -d '{"targetStatus":"IN_USE","expectedVersion":0,"effectiveDate":"2026-02-10","usefulLifeMonths":36}' \
  http://localhost:8080/api/assets/7/transitions

# 计提 2026-02
curl -u finance:Pass#2026 -H 'Content-Type: application/json' -H 'X-Request-Id: demo-3' \
  -d '{"period":"2026-02"}' http://localhost:8080/api/depreciation/runs

# 解释
curl -u viewer:Pass#2026 "http://localhost:8080/api/assets/7/depreciation?throughPeriod=2026-12"
```
