# 本地知识抽取后端 · Local Knowledge Extraction Backend

纯本地、可解释的中文/英文文本知识抽取服务。**不接入任何外部模型或网络 API**，
实体识别只依赖随服务发布的词典与正则规则，抽取结果可逐条回溯到「哪条规则、
原文哪个位置」。

技术栈：Flask + SQLAlchemy + SQLite（WAL）+ 最多 2 个本地后台工作器，Docker 一键启动。

## 能力总览

| 需求 | 实现 |
| --- | --- |
| 工作区隔离、SHA-256 内容去重 | 所有业务表都带 `workspace_id`；内容存全局 `blobs` 表按哈希去重，多工作区共用一份字节但各持一行文档与权限 |
| 规则发布后不可变 | `rule_packs` 只追加；版本号由内容 SHA-256 派生，不可修改 |
| 作业绑定文档摘要与规则版本 | `jobs.doc_sha256` + `jobs.rule_pack_version`；文档改版或规则升级后旧作业迟到结果一律拒绝落库 |
| 四类实体 + 原文位置 + 规则 + 规范名 | person / org / tech / date；存 Unicode 码点起止、`rule_id`、`canonical`、原文片段；别名归一只写 `canonical`，原始提及完整保留在 mention 里 |
| TF-IDF + 倒排索引工作区内搜索 | 明确分词（CJK 单字 + 拉丁/数字串）、明确权重（sublinear TF × 对数 IDF，余弦归一）、稳定排序（score→doc_id→char_start 三级） |
| 索引代次隔离 | 每代索引一行 `index_generations`，查询只走 `status='active'` 的那一代，切换在单个事务内完成，绝不混读新旧代 |
| 最多 2 个工作器 | `worker_leases` 租约表，DB 级互斥，超出的进程不抢任务 |
| 作业 / 检查点持久化、重启恢复 | 作业与检查点落库；工作器启动时回收过期租约，`pending/failed/running(僵尸)` 自动继续 |
| 失败可重试、可定位 | 错误信息含 `doc_id` 与 `stage`（extract/index/apply）；POST 重试接口 |
| 重建不漏新文档 | 重建结束前对 `documents.id <= build 完成时刻` 做覆盖校验，缺漏立即补建，通过后才切换 active |
| 重复执行不重复生成 | entities 有唯一约束；倒排表 upsert；作业去重键 |
| 回滚不改写证据 | 回滚只切规则指针和索引代次；历史 entities/mentions 永不更新删除（仅文档删除时级联） |
| 删除不泄露 | 文档删除在一个事务内清掉实体/mentions/倒排/计数器，并在 blob 引用归零后清除内容字节；随后删除的内容也从事务里挡住正在跑的作业 |
| 迁移 / 文档 / 样例 / 测试 | `migrations/` + 启动自动迁移；本 README；`demo.sh`；`tests/` |

## 快速开始（Docker）

```bash
docker compose up --build
# API: http://localhost:8080
```

容器启动时自动执行迁移并载入内置规则包 `builtin-v1`，API 进程内置 2 个工作器线程。
数据持久化在命名卷 `kex_data`（SQLite WAL 三件套都在 `/data`）。

也可以只起 API、另起工作器容器（工作器仍受租约上限 2 约束）：

```bash
KEX_API_WORKER_THREADS=0 docker compose --profile worker up --build
```

此时 API 进程不跑工作器线程，由 `worker` 容器内最多 2 个工作器处理任务。
即使不设置该变量、API 和 worker 容器都带工作器，租约仍由数据库认领事务
**全局**限制在 2，多起进程/容器不会超并发（注册数 ≠ 租约数）。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export KEX_DB_URL=sqlite:///./kex.db
flask --app app:create_app init-db          # 迁移 + 内置规则
flask --app app:create_app run --port 8080  # 另开进程: python -m app.worker
pytest -q
```

## 30 秒跑通样例

```bash
./demo.sh                    # 需要运行中的服务（默认 http://localhost:8080）
```

脚本会：建工作区 → 上传一篇真实技术新闻文本 → 轮询抽取作业 →
打印人名/组织/技术名/日期实体（带位置和命中规则）→ 再发第二篇文档 →
等索引重建 → 演示搜索、关键词、规则升级（v2 新增别名）、规则回滚、导出。

也可以不依赖服务，直接跑纯本地的抽取演示：`python -m app.demo_offline`。

## API

除 `/healthz` 外所有接口都需要请求头 `X-Workspace-Key`（建工作区时返回的密钥）。
管理规则包用 `X-Admin-Key`（默认 `dev-admin-key`，生产用 `KEX_ADMIN_KEY` 覆盖）。

### 工作区与文档

```
POST /api/workspaces                     {name}                       -> {id, api_key}
POST /api/documents                      multipart: file=<utf8文本>     -> {doc_id, sha256, job_id}
GET  /api/documents                                                   -> [{id, name, sha256, created_at}]
GET  /api/documents/<doc_id>                                          -> 元数据
GET  /api/documents/<doc_id>/content                                  -> 原文（未删除才可见）
DELETE /api/documents/<doc_id>                                        -> 事务级硬删除派生数据
GET  /api/documents/<doc_id>/entities                                 -> 四类实体 + 位置 + 规则 + 别名
GET  /api/documents/<doc_id>/keywords?top_k=10                        -> TF-IDF 关键词
GET  /api/export?format=ndjson                                        -> 全工作区导出（实体含原文证据）
```

同名同内容再次上传返回去重结果（`deduplicated: true`），跨工作区上传相同内容：
字节共享（blob 引用计数），但密钥不通——A 区密钥永远读不到 B 区文档。

### 抽取与作业

文档上传后自动入队 `extract` 作业，绑定当前内容哈希与规则版本。

```
GET  /api/jobs?status=failed                            -> 作业列表（含失败阶段）
GET  /api/jobs/<job_id>                                 -> 状态、attempts、error、checkpoint
POST /api/jobs/<job_id>/retry                           -> 失败作业重新入队
```

### 搜索（倒排索引 / TF-IDF）

```
GET /api/search?q=...&top_k=10
```

- **分词**：CJK 统一表意文字逐字成词；拉丁字母/数字按连续串成词（保留
  `snake_case`、`camelCase`、`x.y.z`、`v1.2.3` 等标识符形态）；小写化。
- **停用词**：一份显式、可审计的功能字/功能词表（见 `app/tokenizer.py` 的
  `STOPWORDS`，如「的/了/在/与/和/中」和 `the/a/and`）在索引和查询时**同时**
  剔除，因此不影响命中一致性；实体识别直接扫描原文，不受停用词影响。
- **权重**：`tf = 1 + ln(count)`（词频为 0 时为 0）；
  `idf = ln((N + 1)/(df + 1)) + 1`；文档向量 L2 归一。查询词等权（1/√n）。
- **打分**：文档与查询归一化向量点积；命中位置随 posting 返回，用于片段高亮。
- **稳定排序**：`score DESC, doc_id ASC, first_char_start ASC`。
- **中文多字检索**：逐字成词 + 查询 AND，「北京大学」要求四个字都出现于同一文。
- 只检索当前 active 索引代；同一代内文档覆盖完整（见下）。

### 规则包发布、升级与回滚

规则包是 JSON（结构见 `rules/builtin_v1.json`），只允许新增，不能改已发布内容。

```
POST /admin/rule-packs        multipart: file=<pack.json>     -> {version}（内容哈希派生）
POST /admin/workspaces/<id>/activate-rule   {version}         -> 全量重抽 + 全量重建索引
POST /admin/workspaces/<id>/rollback-rule   {version}         -> 切回完整旧代，或为旧规则重建
GET  /admin/workspaces/<id>/rule                            -> 当前/历史激活记录
GET  /admin/rule-packs                                      -> 所有已发布版本
```

- 激活新版本：为每篇文档创建绑定新规则版本的 extract 作业（旧证据保留），
  并启动新一代索引 `building`；覆盖校验通过后事务内切为 `active`，旧代转 `superseded`。
- 回滚：若该规则版本已有覆盖当前全部文档的完整索引代，原子切回；否则用旧规则新建一代重建。
  **任何情况下不删除、不改写历史 entities/mentions**——导出仍带 `rule_pack_version` 可审计。

## 失败阶段与恢复语义

| 阶段 | 含义 | 可定位信息 |
| --- | --- | --- |
| extract | 读 blob、跑规则、写 entities/mentions | doc_id、规则版本、内容哈希 |
| index | 分词、计算 TF、upsert 倒排 | doc_id、代次 |
| apply | 覆盖校验 + 代次切换 | workspace_id、generation |

作业崩溃 → 租约 30 秒过期 → 被重新认领，`attempts` 累加；超过 5 次置 `failed`，
可 POST retry 手动重置。检查点（已处理 doc_id / generation）持久化，重复执行幂等。

## 运行测试

```bash
pytest -q
```

覆盖：别名位置保留、规则版本隔离、并发重建不漏不混、重启恢复（杀进程模拟）、
删除竞争（抽取中删除）、跨工作区访问拒绝、TF-IDF 排序确定性与回滚审计。

## 目录

```
app/            Flask 应用、ORM、规则引擎、索引、工作器、API
rules/          内置规则包（可解释词典/正则）
migrations/     手写 SQL 迁移（0001 与 ORM 模型一一对应）
tests/          pytest
Dockerfile docker-compose.yml
demo.sh         端到端样例
```
