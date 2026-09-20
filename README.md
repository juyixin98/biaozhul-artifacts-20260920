# ConsentVault

可追溯的同意记录后端（FastAPI + SQLAlchemy + PostgreSQL）。
**本系统只提供可追溯的授权状态记录，不宣称满足任何具体法规认证。**

## 核心语义

- 同意按 **组织 / 主体 / 目的** 记录，支持授予（grant）、撤回（withdraw）与到期（expires_at）。
- 政策版本发布后不可修改（无更新/删除接口）；每次授予绑定具体版本，发布新版本**不自动续授权**（`verify` 中以 `policy_stale` 标示）。
- 写入携带 `event_id` + `expected_version`：
  - 重复请求（同 ID 同内容）返回首次结果（`replayed: true`）；
  - 同 ID 不同内容 → `409`；`expected_version` 与当前版本不符 → `409`；
  - 校验失败的事件不会进入 `consent_events` 历史（整事务回滚）。
- `GET /consents/verify` 实时计算有效性：到期立即失效，不依赖清理任务。
  撤回后旧授权不会因延迟重试恢复（重试命中幂等返回原结果，过期版本返回 409），只有新的明确授予可以恢复。
- 状态投影可由不可变事件历史重建（`POST /consents/rebuild`），结果与增量处理一致；
  重建期间的新事件通过条件 upsert（`version < excluded.version`）保证不丢失。
- 批量导入 `POST /consents/events/batch` 最多 500 条，任意错误整批回滚。
- 删除主体（`DELETE /subjects/{ref}`）清除可识别映射与全部导出副本；
  审计日志只保留不含个人字段的操作记录（事件历史中仅剩不可再关联的 `subject_id`）。
- 角色分离：`admin` 可写，`auditor` 只读；所有查询按组织隔离。

## 启动（Docker）

```bash
sudo docker compose up -d --build        # 自动执行 alembic 迁移
sudo docker compose exec app python -m scripts.seed   # 初始化演示组织/用户/政策
python -m scripts.demo                   # 在本机跑端到端演示（默认打 http://localhost:8010）
```

服务端口：API `8010`，PostgreSQL `55435`（避免与本机已占用的 5432/8000 冲突）。

演示 token（由 seed 生成）：`acme-admin-token`（admin）、`acme-auditor-token`（auditor）、
`globex-admin-token`（另一组织，用于隔离演示）。

## 测试

```bash
pip install -r requirements.txt
pytest tests/ -v        # 需要 55435 上的 PostgreSQL（compose 的 db 服务）
```

覆盖：并发撤回、过期边界、政策更新、重复导入、历史重建（含重建期间并发写入）、
删除后的查询结果、权限与跨组织隔离。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/consents/events` | 记录授予/撤回（幂等 + 乐观版本） |
| POST | `/consents/events/batch` | 批量导入（≤500，整批回滚） |
| GET | `/consents/verify` | 当前有效性 + 依据事件与政策版本 |
| GET | `/consents/history` | 不可变事件历史 |
| POST | `/consents/rebuild` | 从历史重建状态投影 |
| POST | `/purposes/{code}/policy-versions` / `/policy-versions/{id}/publish` | 政策版本（发布后不可变） |
| POST/GET | `/subjects/{ref}/exports` | 生成/查看导出副本 |
| DELETE | `/subjects/{ref}` | 删除主体（清除映射与导出副本） |
| GET | `/audit-log` | 审计日志（无个人字段） |
