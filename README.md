# Meridian TravelOps — 合同结算后端

PHP 8.2 · Slim 4 · Eloquent (illuminate/database) · MySQL 8 · Docker Compose

覆盖范围：合同模板生成与版本化签署、供应商结算明细导入、版本化规则成本分摊、
每日关账与纠错。不包含预订、支付接入或外部通知。

## 快速开始

```bash
docker compose up --build        # 迁移 + 种子数据 + 服务于 :8081
docker compose exec app vendor/bin/phpunit          # 完整测试（含 MySQL 并发竞争测试）
```

无 Docker 时（sqlite，跳过 fork 竞争测试）：

```bash
composer install
php bin/migrate.php
vendor/bin/phpunit
DB_DRIVER=sqlite php -S 127.0.0.1:8080 -t public
```

API 详见 [docs/API.md](docs/API.md)。

## 设计要点

### 合同与签署
- 合同由模板 + 变量渲染；**发起签署时**冻结完整内容与 sha256 摘要
  （`contract_versions.content` / `content_hash`）。
- 签署人按 `seq` 严格顺序确认，每次确认绑定具体版本行；内容变更产生新版本，
  旧版本置为 `superseded`，其确认记录**不可沿用**。
- 首次确认前可撤回；签署窗口 72 小时，过期惰性生效（confirm/withdraw/expire
  路径都会触发）。
- 并发控制：版本行 `SELECT ... FOR UPDATE` + 带状态守卫的原子 UPDATE。
  签署/撤回/到期竞争只会按一个有效状态推进；重复确认走幂等路径返回既有结果，
  不产生重复行或重复事件。
- 操作者与时间保存在 `contract_signers` 与 `contract_events`——仅为运营审计，
  **不构成法律认证**。

### 结算明细导入
- 金额以定点小数字段存储（`amount_minor` bigint），API 收十进制字符串，
  按币种小数位换算，全程无浮点。
- 按合同、币种、业务日期归集（`settlement_lines` 上有对应索引）。
- `external_ref` 唯一约束去重：同 ID 同内容 → 幂等跳过；同 ID 不同内容 →
  `409 external_ref_conflict`；任何校验失败或冲突**整批回滚**，批次行不留痕。

### 成本分摊
- 分摊规则按名称版本化、不可变；结算单引用具体规则版本。
- 份额 = `floor(总额 × 权重 / 总权重)`，尾差全额归入 `remainder_target`
  （默认第一个目标），分摊行上 `is_remainder_sink` 明确标记尾差归属，
  分摊总和恒等于原金额（含负数总额）。
- 结算单必须绑定**已完成签署**的合同版本，否则 `409`。

### 每日关账
- `daily_closes.close_date` 唯一约束 + 幂等返回；关账后该日期的导入、结算、
  纠错目标日均被拒绝。
- 关账与导入/结算通过 MySQL 命名锁（`GET_LOCK('meridian:close:<date>')`）
  互斥：不存在漏记或半关账状态。
- 关账后记录不可改；纠错以**冲销/调整**记录落在更晚的开放日，原始记录不变。

## 测试

`tests/` 覆盖：签署顺序与版本绑定、72h 到期边界、重复确认幂等、
签署/撤回并发竞争（pcntl_fork + MySQL）、重复导入与同 ID 冲突整批回滚、
分摊尾差归属与总额守恒、关账边界（幂等关账、关账后拒绝、冲销落下一开放日、
关账/导入竞争）。

## 目录

```
bin/            migrate / seed 脚本
database/       迁移（PHP，按文件名顺序执行）
docs/API.md     接口说明
public/         Slim 入口
src/Models      Eloquent 模型
src/Services    ContractService / ImportService / SettlementService /
                AllocationService / CloseService
src/Support     Database / Clock / Money(定点) / LockManager / Migrator
tests/          PHPUnit
```
