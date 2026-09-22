# KEX — 本地知识抽取后端

纯本地、可解释的知识抽取与检索服务：**Flask + SQLAlchemy + SQLite(WAL) +
Docker**。不调用任何外部模型，只处理 UTF-8 文本，实体识别完全由内置词典与
确定性规则完成，每条抽取证据都可回溯到具体规则与原文位置。

---

## 1. 能力概览

| 需求 | 实现 |
| --- | --- |
| 文档工作区隔离 + SHA-256 去重 | `documents` 为全局内容寻址 blob；访问一律经 `workspace_documents` 关联表，相同内容可复用文件但权限不串 |
| 规则发布后不可变 | `rule_versions.snapshot/checksum` 一经发布冻结；改版=插入新版本，回滚=切指针或用旧规则重建 |
| 作业绑定文档摘要与规则版本 | 每个 `job_items` 同时固化 `document_sha256` 与 `rule_version_id`，处理前再次校验 |
| 人名 / 组织 / 技术名 / 日期 | Trie 词典（含别名归一并保留原文）+ 技术名正则 + 中英文日期正则（ISO / 斜杠 / 中文 / 英文月份） |
| 原文起止位置、规则、规范名 | 每条 `entities` 记录 `start_char/end_char/text/canonical_name/matched_rule` |
| TF-IDF + 倒排索引 | 确定性分词（拉丁词 + CJK 一元/二元）、平滑 IDF、次线性 TF、cosine 打分；稳定排序 `score DESC, document_id ASC` |
| 搜索只看同一代完整索引 | 索引分代（generation）；重建期查询继续读旧 active 代，重建完成后**原子切换**，绝不混读新旧代 |
| 最多 2 个本地工作器 | 集群心跳表 `worker_heartbeats` + 条件 UPDATE 原子认领，硬性 cap（默认 2） |
| 作业/检查点持久化、重启恢复 | `job_items.stage/status/leased_until` 落库；租约过期自动重新认领；阶段级检查点 |
| 重建期新文档不遗漏 | 上传时向 active 代与**所有 building 代**各排一个 item |
| 重复执行不重复生成 | 实体唯一约束 + 存在性检查；索引 item 幂等；内容去重不重复建作业 |
| 失败可重试、可定位文档与阶段 | `POST /jobs/{id}/retry`；失败项含 `document_id/stage/error/attempts` |
| 旧作业迟到结果不覆盖新版本 | 激活重建时校验当前规则指针；不一致则该代标 `retired`；item 写入前校验摘要与代归属 |
| 规则回滚不改写历史证据 | 回滚只切指针/新建代；旧实体行与旧代完整保留 |
| 删除后不泄露 | 一个事务内从**所有代**清除 posting/统计/实体/排队项，再删关联，blob 引用计数归 0 才删除 |
| 迁移 / 文档 / 样例 / 测试 | Alembic 迁移；本文档 + API 文档；CLI 样例语料；48 个 pytest 用例 |

---

## 2. 快速开始（Docker，推荐）

```bash
docker compose up --build
```

启动内容：

- `web` 容器：`alembic upgrade head` 后用 gunicorn 提供 `:8000` API，并内嵌
  1 个工作器线程（`KEX_EMBED_WORKER=1`）；
- `worker` 容器：独立工作器进程。两者合计**正好 2 个本地工作器**（硬上限）；
- SQLite 数据库位于命名卷 `kex-data:/data/kex.db`，自动以 WAL 打开。

验证：

```bash
curl -s http://localhost:8000/health
# {"journal_mode":"wal","status":"ok"}
```

### 只运行单容器（含内嵌工作器）

```bash
docker build -t kex-local .
docker run -p 8000:8000 -e KEX_EMBED_WORKER=1 -v kex-data:/data kex-local
```

---

## 3. 本地开发（无 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export KEX_DB_PATH=$PWD/data/kex.db
alembic upgrade head

# 终端 A：API（也可加 KEX_EMBED_WORKER=1 内嵌工作器）
flask --app kex.app run --debug   # 或 gunicorn "kex.app:create()"

# 终端 B：独立工作器（最多与内嵌线程合计 2 个）
python -m kex.cli worker
```

### 可运行样例（不返回固定结果）

样例语料见 `kex/services/demo_corpus.py`（中英混合、含别名/多日期格式）：

```bash
python -m kex.cli seed-demo demo       # 建工作区 + 上传 3 篇真实文本，返回 api_key
python -m kex.cli worker               # 另开终端，跑完作业后 Ctrl-C
python -m kex.cli list-workspaces      # 查看工作区
```

也可直接运行端到端脚本（建库→上传→抽取→检索→导出）：

```bash
bash examples/sample_requests.sh
```

---

## 4. API 速览

所有工作区路由都需要请求头 `X-Workspace-Key: <创建时返回的 key>`。
完整字段见 [`docs/API.md`](docs/API.md)。

```
POST   /api/workspaces                              # 创建工作区（返回一次性 api_key）
POST   /api/workspaces/{ws}/documents               # 上传文本（JSON 或 text/plain；仅 UTF-8）
GET    /api/workspaces/{ws}/documents               # 列表
GET    /api/workspaces/{ws}/documents/{id}          # 原文
DELETE /api/workspaces/{ws}/documents/{id}          # 删除（全代清除，见上）
GET    /api/workspaces/{ws}/entities                # 实体（可按文档/类型/规则版本过滤）
GET    /api/workspaces/{ws}/entities/grouped        # 规范名分组（保留每个原始提及）
GET    /api/workspaces/{ws}/export                  # NDJSON 全量导出
GET    /api/workspaces/{ws}/search?q=...            # TF-IDF 检索（仅 active 代）
GET    /api/workspaces/{ws}/documents/{id}/keywords # 单文档 TF-IDF 关键词
GET    /api/workspaces/{ws}/rules                   # 规则版本列表
POST   /api/workspaces/{ws}/rules/publish           # 发布不可变新版本并重建索引代
POST   /api/workspaces/{ws}/rules/rollback          # 回滚到旧代或用旧规则重建
GET    /api/workspaces/{ws}/jobs                    # 作业列表（含分阶段检查点）
GET    /api/workspaces/{ws}/jobs/{id}               # 作业详情（失败定位文档/阶段）
POST   /api/workspaces/{ws}/jobs/{id}/retry         # 重试失败 item
GET    /health                                      # 健康检查（确认 WAL）
```

### 规则集 JSON 结构

```json
{
  "gazetteer": {
    "PERSON": [{"canonical": "李明", "aliases": ["小李", "明哥"],
                "rule_id": "gazetteer.person.liming", "priority": 100}],
    "ORG":    [{"canonical": "Acme Corp", "aliases": ["Acme"]}],
    "TECH":   [{"canonical": "Apache Kafka", "aliases": ["Kafka"]}]
  },
  "tech_patterns": [
    {"pattern": "(?<![A-Za-z])Kafka\\s+\\d+(?:\\.\\d+)+(?!\\d)",
     "rule_id": "tech_pattern.kafka_version", "priority": 120}
  ],
  "date_rules_enabled": true
}
```

- 发布时服务端做规范化（排序/去重/校验）并计算 SHA-256 校验和；
- 同一规则内容重复发布是幂等的，返回既有版本、不触发重建；
- 日期规则（`date.iso / date.slash / date.cn / date.english`）内置不可由用户
  改写，规范名统一为 `YYYY-MM-DD`；非法日期（如 `2023-13-40`）不会产出。

---

## 5. 检索：分词、权重与排序（确定性）

**分词**（见 `kex/tfidf.py`）

1. 拉丁字母/数字串整体小写，数字间 `.` 保留（`"3.6"`、`"3.12.1"` 是一个词）；
2. CJK 字符同时生成一元与重叠二元：`清华大学 → 清华/华大/大学 + 清/华/大/学`；
3. 其余字符（标点、空白）为分隔符。

**权重**

- `tf`：词在文档内出现次数；
- `idf = ln((N - df + 0.5)/(df + 0.5) + 1)`（sklearn 式平滑 IDF）；
- posting 权重 `w = (1 + ln(tf)) * idf`（次线性 tf）；
- 文档向量范数 `sqrt(Σ w²)`；
- 查询得分 = cosine `Σ(wq·wd)/(‖q‖·‖d‖)`。

新增/删除文档会在同一事务内重算受影响词的 df/idf、历史 posting 权重与范数，
所以索引始终严格符合上述定义。

**稳定排序**：`score DESC, workspace_document_id ASC`。相同分数顺序固定、可复现。

**分代一致性**：每次规则发布创建一个 `building` 代并全量重建；查询只读
`workspace_states.active_index_generation_id` 指向的唯一 active 代。重建成功的
瞬间切换指针、旧代置 `retired`；若期间用户又发布/回滚导致规则指针已变，则迟到
重建不激活（置 `retired`），因此不会出现新旧代混读或旧结果覆盖。

---

## 6. 并发与可靠性模型

- **认领原子性**：工作器先选候选 item，再用条件 `UPDATE ... WHERE status=queued
  OR (running AND leased_until<now)` 抢占，SQLite 写锁保证两个进程不会同时拿到
  同一 item。
- **2 工作器上限**：`worker_heartbeats` 表成员计数；超过 cap 的工作器注册失败并
  周期性重试。租约默认 60 秒；崩溃工作器的 item 租约过期后由他人恢复。
- **检查点**：item 的 `stage` 为 `queued → extract → index → done`，每阶段独立
  提交。失败后重试从检查点继续；作业详情给出失败的 `document_id`、`stage`、
  `attempts` 与错误信息。
- **写冲突重试**：两工作器并发写同一代时，SQLite 的 `BUSY` 或 term 唯一竞争会
  触发 item 级乐观重试（抽取/索引均幂等，重试安全）。
- **幂等**：实体按 `(ws, doc, rule_version, start, end, type)` 唯一；索引按
  `(generation, doc)` 的统计行存在性短路；同工作区重复上传相同 SHA-256 内容只
  复用、不再排队。
- **删除竞争**：item 被认领后若文档被删，工作器在任何写入前检测到关联消失，将
  item 标 `skipped`，绝不补写实体或 posting。

---

## 7. 迁移

```bash
alembic upgrade head     # 升级（容器入口自动执行）
alembic downgrade -1     # 回滚一个版本
```

所有迁移在打开连接时设置 `journal_mode=WAL / foreign_keys=ON /
synchronous=NORMAL / busy_timeout=30000`。

---

## 8. 测试

```bash
pip install pytest
python -m pytest -q
```

覆盖：别名位置偏移、规范名归一保留原文、四类实体、自定义词典动态生效、
版本隔离与回滚不改写证据、**双工作器并发重建**、租约过期**重启恢复**、
失败注入后重试且不重复、**删除竞争**、跨工作区 blob 复用隔离、删除后检索/实体/
导出无泄露、分代搜索不混读，以及真实 Alembic 迁移建库与 WAL 校验。

---

## 9. 目录结构

```
kex/
  app.py                Flask 工厂（可选内嵌工作器）
  config.py db.py       配置 / 引擎(SQLite pragma)
  models.py             ORM 模型
  extraction.py         Trie 词典 + 日期规则（可解释引擎）
  tfidf.py              分词 / TF-IDF
  services/
    workspaces.py       建工作区/鉴权
    documents.py        上传去重 / 删除清除
    rules.py            发布(幂等) / 回滚
    jobs.py             作业调度 / 工作器循环 / 检查点
    search_index.py     分代倒排索引 / 检索 / 关键词
    entities.py         实体查询 / 分组 / 导出
  web/api.py            HTTP 路由
  cli.py                worker / init-db / seed-demo
alembic/                迁移
examples/               可运行样例脚本
tests/                  pytest
```
