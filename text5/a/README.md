# DeskLens — 员工活动汇总后端

接收监控代理上报的**每分钟活动快照**(不采集原始击键),按隐私策略过滤、分类并汇总为员工每日 / 部门每周报表。Go + Echo + sqlx + PostgreSQL。

## 快速启动

```bash
docker compose up --build        # API 监听宿主机 :18080,Postgres :15432(自动迁移 + 样例数据)
docker compose --profile test run --rm test   # 运行全部测试(含集成测试)
```

宿主机端口避开了常见的 8080/5432 占用;容器网络内仍是 app:8080、db:5432。

本地开发:`createdb desklens && DATABASE_URL=postgres://... go run ./cmd/server`;
测试:`TEST_DATABASE_URL=postgres://.../desklens_test go test ./...`(未设置时集成测试自动跳过)。

## 数据流

```
agent ──POST /v1/snapshots──▶ 校验(整批) ─▶ 隐私过滤(策略版本N) ─▶ 分类(规则版本M)
                                    │                │
                                    ▼                ▼
                              raw_snapshots ◀── 幂等键 (workstation, minute)
                                    │ 同事务重算受影响 员工日 / 部门周
                                    ▼
                     daily_summaries / weekly_summaries
```

## 关键设计

**幂等摄取。** 原始表以 `(workstation_id, minute_utc)` 为唯一键,每行存内容哈希(员工/分钟/应用/计数)。相同内容重复上报 → 计为 duplicate,只处理一次;同键不同内容 → 整批回滚并返回 409;任一记录校验失败(未知工作站、员工不匹配、负计数等)→ 整批 400,什么都不写。

**隐私过滤。** 摄取时加载当前策略版本:豁免部门、匹配排除通配符的应用、员工本地时区监控窗口(`work_start`–`work_end`)之外的快照**直接丢弃,不进入原始表**,因此也不可能进入任何统计。每个落库行记录实际使用的 `policy_version`;发布新策略只影响之后的数据。

**分类。** 应用名按本地通配规则(`*`/`?`,大小写不敏感)匹配,优先级高者胜,**优先级相同按规则 ID 升序**稳定取胜;无匹配为 neutral。分类结果与 `rules_version` 固化在原始行上——发布新规则集只影响新数据,历史不会被悄悄改写。

**汇总与重建。** 每日/每周汇总不做增量加减,而是**从原始行整体重算单个键**(`INSERT ... ON CONFLICT DO UPDATE`),并用 `pg_advisory_xact_lock` 按键串行化。因此:
- 迟到快照只触发其所属 `local_date`(及所在部门周)的重算;
- 全量重建与增量处理是同一段代码,结果必然一致;
- 重建与摄取并发时,同一员工日的写入在 advisory lock 下串行,不会丢失或重复累计。

**清理边界。** `POST /v1/admin/cleanup {before}` 删除 `local_date < before` 的原始行,汇总保留,并把水位线 `system_state.raw_retained_from` 推进到 `before`。凡是重建范围触及水位线之前的请求一律拒绝(409)——残缺历史永远覆盖不了完整统计。

**隔离。** Bearer token 认证(`api_tokens` 表)。员工只能看自己;经理只能看本部门(日报、周报、**明细接口与 CSV 导出同样隔离**);管理员可发布策略/规则、重建、清理。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/snapshots` | 批量上报快照,返回 accepted/duplicates/filtered 及所用策略、规则版本 |
| GET | `/v1/employees/:id/daily?from&to` | 员工每日汇总(本人/本部门经理/管理员) |
| GET | `/v1/departments/:id/weekly?from&to` | 部门每周汇总(本部门经理/管理员) |
| GET | `/v1/snapshots?employee_id&from&to` | 原始明细,同样按部门隔离 |
| GET | `/v1/departments/:id/export.csv?from&to` | 部门日报 CSV 导出 |
| POST | `/v1/admin/policies` | 发布新策略版本 `{work_start, work_end, excluded_apps}` |
| POST | `/v1/admin/rules` | 发布新分类规则集 `{rules:[{pattern, category, priority}]}` |
| POST | `/v1/admin/rebuild` | 重建 `[from, to]` 汇总(触及已清理区间返回 409) |
| POST | `/v1/admin/cleanup` | 清理 `before` 之前的原始数据,保留汇总 |

### 示例

```bash
# 上报快照(样例 token 见 seed/seed.sql)
curl -X POST localhost:18080/v1/snapshots \
  -H 'Authorization: Bearer token-bob' -H 'Content-Type: application/json' \
  -d '{"snapshots":[{"workstation_id":"ws-bob","employee_id":4,
       "timestamp":"2026-01-05T10:00:00Z","app_name":"VS Code","activity_count":42}]}'

# 工程部经理看本部门周报
curl -H 'Authorization: Bearer token-mgr-eng' \
  'localhost:18080/v1/departments/1/weekly?from=2026-01-01&to=2026-01-31'

# 管理员清理 2 月前的原始数据
curl -X POST -H 'Authorization: Bearer token-admin' -H 'Content-Type: application/json' \
  -d '{"before":"2026-03-01"}' localhost:18080/v1/admin/cleanup
```

## 测试

`tests/` 覆盖:隐私过滤(窗口/排除应用/豁免部门 + 策略版本记录)、幂等与整批回滚、跨时区跨日、迟到补传只重算受影响日期、规则版本不改写历史、重建与摄取并发下的一致性、清理水位线边界,以及 HTTP 层部门隔离。`internal/classify` 另有纯单元测试(通配匹配、优先级与规则 ID 稳定决胜)。

## 备注

- 监控窗口按员工本地时间解释,要求 `work_start < work_end`(不支持跨午夜窗口)。
- 周汇总以 ISO 周一为 `week_start`,由每日汇总聚合;每日汇总是唯一从原始表重算的一层。
