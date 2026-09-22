# 本地文本分析任务引擎

基于 **Django REST Framework + MySQL + spaCy + NLTK + Docker Compose** 的本地化文本分析服务。
所有计算都在本机完成，**不接入任何外部模型/大模型服务**；分析结果仅作为人工复核的**线索**，
系统不会也不可能输出“确定由 AI 生成”或“作弊”结论。

---

## 1. 功能概览

| 需求 | 实现 |
|---|---|
| 批量提交 TXT / DOCX | 单批最多 **100** 份、单文件最大 **10 MB**（`analysis/services.py`、`analysis/extraction.py`） |
| 内容摘要去重 | 对“归一化后的纯文本”取 **SHA-256**；同一课程内重复上传记录为 `duplicate`，不再重复分析 |
| 跨课程独立 | 去重唯一键是 `(course, content_sha256)`；相同文本在不同课程是独立文档、独立权限、独立提交记录与任务 |
| 本地分析指标 | 段落长度、词汇丰富度（TTR / MTLD / Honoré）、重复片段（8/12/16 词 n-gram）、风格相似度（功能词 + 词性向量余弦） |
| 数据库任务队列 | 领取（claim）、租约（lease）、心跳（heartbeat）、超时回收、失败重试、进度查询 |
| 并发安全 | MySQL `SELECT … FOR UPDATE SKIP LOCKED` 领取；`worker_id + generation` 代数围栏，旧进程恢复后无法覆盖新结果 |
| 版本化结果 | 结果只追加不修改，绑定 `input_sha256` 与 `algorithm_version`；重跑产生新版本（v1、v2…） |
| 单文档失败隔离 | 单份文件解析失败只影响自身；批内其他文件正常入库、正常分析；失败保留可定位的 `stage` 与错误信息 |
| 教师权限隔离 | 所有查询集按“本人拥有/任教的课程”过滤；跨课程读取返回 404（不可见），写入返回 403/404 |

算法版本：**`1.0.0`**（常量 `analysis/metrics.py: ALGORITHM_VERSION`；
修改任何公式时升级该常量，历史结果仍保留其生成时的版本号）。

---

## 2. 指标与公式（算法版本 1.0.0）

输入先做归一化（NFKC、`CRLF→LF`、折叠行内空白、压缩连续空行、去首尾空白），再分词分析。

### 2.1 段落长度 `metrics_json.paragraphs`
- 段落数、句子数、词数；
- 每段句子数 / 每句词数 / 每段字符数的 `mean / median / stdev / min / max`；
- 句子切分以 spaCy 为主，NLTK punkt 作为独立第二切分器（`sentence_count_nltk` 交叉核对）。

### 2.2 词汇丰富度 `metrics_json.lexical_richness`
令 **N** = 词令牌数，**V** = 不同词形数，**V1** = 仅出现一次的词（hapax legomena）数：

- **TTR（型例比）**：`TTR = V / N`
- **MTLD**（McCarthy & Jarvis, 2010，阈值 **0.720**）：
  从前往后累积，每当 TTR 降到 ≤0.720 记一个 factor；尾部不足一个 factor 时按
  `(1 − TTR_partial)/(1 − 0.720)` 比例计入。反向同样计算，最后
  `MTLD = N / ((F_forward + F_backward) / 2)`。值越大通常词汇越多样。
- **Honoré 统计量**（Honoré, 1979）：`H = 100 * ln(N) / (1 − V1/V)`；
  当 V1 = V（极短文本，分母为 0）时返回 `null`。

### 2.3 重复片段 `metrics_json.repeated_fragments`
- 对小写字母词序列分别取 **8、12、16** 词 n-gram；
- 输出每个 n 下“重复的不同 n-gram 数”“重复事件数”；
- `repeated_token_share` = 被任意重复 n-gram 覆盖的词令牌比例（0–1，越高自我重复越多）；
- `top_fragments`：按 n 从长到短、出现次数从多到少，列出最多 5 条**可读片段**。

### 2.4 风格相似度 `metrics_json.style_similarity`
- 每篇文档生成风格向量：固定的英语功能词表（约 120 个）相对词频 +
  粗粒度 UD 词性相对频率；两半各自 **L2 归一化**后拼接，使两族特征权重相等；
- 与**同一课程内**所有“内容摘要不同”的既有文档最新结果计算**余弦相似度**；
- 返回 `mean_cosine / max_cosine` 及最接近的 5 个样本；
- **样本不足处理**：可参照样本少于 **3** 份时，返回
  `status = "insufficient_samples"`，不强行给出分数（阈值可在
  `settings.ANALYSIS["STYLE_MIN_REFERENCE_SAMPLES"]` 调整）。

### 2.5 小样本处理
- 句子数 < 2：`words_per_sentence` 置 `null`，给出 warning；
- 词令牌数 < 20：TTR/MTLD/Honoré 仍输出，但标记 `small_sample: true`（此类短文本指标不稳定）；
- DOCX/TXT 为空或无法解析：文件槽记录为 `rejected`，并给出定位信息，不产生任务。

> 所有结果载荷都带 `disclaimer`：指标是供人工复核的启发式线索，**不能**作为
> “AI 生成/作弊”的判定证据。

---

## 3. 任务队列与并发正确性

代码：`analysis/queue.py`、`analysis/pipeline.py`、`analysis/management/commands/run_worker.py`

- **领取**：单事务内 `SELECT … FOR UPDATE SKIP LOCKED`（MySQL），先取最早的
  `pending`（FIFO），再取租约过期的 `running`；两个 worker 不会拿到同一行。
- **租约 + 心跳**：领取时写入 `leased_at / lease_expires_at`（默认 120s），
  工作线程按 `WORKER_HEARTBEAT_SECONDS`（默认 30s）续租；心跳若发现任务已易主则返回失败。
- **代数围栏（防旧进程覆盖）**：每次领取 `generation += 1` 并生成新的 `worker_id`。
  完成/失败/心跳/阶段更新都必须同时携带 `(worker_id, generation)`，
  校验通过才写入。因此：worker 中断 → 租约到期 → 任务被回收重算 →
  旧 worker 恢复后提交结果会被**拒绝**，不会覆盖新结果。
- **重试**：失败任务在 `attempts < max_attempts(=3)` 时回到 `pending`；
  达到上限置 `failed` 终止，并保留 `error_stage`（extract/parse/metrics/style/persist）
  与 `error_message`。
- **不阻断整批**：每个文件独立建任务，单个任务失败不影响批内其他文档。
- **进度查询**：`GET /api/batches/{id}/` 的 `progress` 给出
  documents/pending/running/succeeded/failed/percent；`GET /api/tasks/` 支持
  `status / stage / document` 过滤。

---

## 4. 结果版本化

- `AnalysisResult` 只追加：唯一约束 `(document, version)`，`version` 从 1 起；
- 每行绑定 `input_sha256`（生成该结果所用文本摘要）与 `algorithm_version`；
- `POST /api/documents/{id}/rerun/` 创建新任务，完成后产生 **v2、v3…**，旧版本保留；
- 同一文档已有未完成任务时，重跑请求返回 **409 Conflict**。

---

## 5. 快速开始（在线构建）

前置：Docker、Docker Compose。

```bash
cp .env.example .env          # 可修改数据库口令等
docker compose up -d --build  # 自动执行 migrate；worker 会等待数据库就绪
docker compose exec web python manage.py seed_demo   # 载入 samples/ 并打印真实指标
```

服务地址：`http://localhost:8000/`，管理后台 `/admin/`。
演示账号由 `seed_demo` 创建：`demo_teacher / demo-password-123`。

扩容 worker（多进程）：

```bash
docker compose up -d --scale worker=3
```

### 5.1 API 速览（JWT）

```bash
# 1) 登录拿 token（可用 seed_demo 的演示账号，或先 /admin 建用户）
curl -s -X POST http://localhost:8000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo_teacher","password":"demo-password-123"}'

# 2) 建课程
curl -s -X POST http://localhost:8000/api/courses/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Biology 101"}'

# 3) 批量上传（multipart，字段 files 可重复，最多 100 个）
curl -s -X POST http://localhost:8000/api/batches/upload/ \
  -H "Authorization: Bearer $TOKEN" \
  -F course=1 -F note="week 3" \
  -F files=@samples/sample1_photosynthesis.txt \
  -F files=@samples/sample2_french_revolution.txt

# 4) 查进度 / 文档结果 / 重跑
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/batches/1/
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/documents/1/
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/documents/1/rerun/
```

上传响应对每个文件槽给出 `created / duplicate / rejected` 及原因（重复与拒收都可见、可定位）。

---

## 6. 离线（内网/无外网）部署

在**有网机器**上准备离线依赖包：

```bash
scripts/download_offline_deps.sh
```

产物（随仓库一起拷到内网机器）：

```
vendor/wheels/     # 全部 Python 包（pip download，含传递依赖）
vendor/models/     # en_core_web_sm-3.7.1 spaCy 模型 wheel
vendor/nltk_data/  # punkt / punkt_tab / stopwords（tokenizers/、corpora/ 目录）
```

内网机器上：

```bash
OFFLINE_BUILD=1 docker compose build    # Dockerfile 用 pip --no-index 离线安装
docker compose up -d
```

> Dockerfile 在 `OFFLINE=1` 时只从 `vendor/` 安装，运行期也不会访问网络。
> MySQL 镜像本身需提前在内网镜像仓库可用（或 `docker save / load` 导入）。

### 迁移

- 首次启动 web 容器自动 `migrate`；
- 手动方式：`docker compose exec web python manage.py migrate`；
- 迁移文件已提交在 `analysis/migrations/0001_initial.py`。

---

## 7. 固定样本文本与真实指标

`samples/` 下有 5 份固定文本（3 篇规范学术短文、1 篇口语化学生随笔、1 篇正式报告）。
`seed_demo` 把它们载入同一课程并**同步走真实分析管线**。一次真实运行的输出
（MySQL 8 + en_core_web_sm 3.7.1，算法 1.0.0）：

```
== sample1_photosynthesis.txt ==
  paragraphs=2  sentences=8  words=121
  words/sentence mean=15.125 stdev=3.689
  TTR=0.7107  MTLD(0.720)=98.92   Honoré=2291.32  hapax=68
  repeated-token-share=0.0  8-gram repeats=0
  style: insufficient samples (0/3 references)

== sample2_french_revolution.txt ==
  paragraphs=2  sentences=7  words=111
  words/sentence mean=15.857 stdev=3.563
  TTR=0.7658  MTLD(0.720)=132.69  Honoré=5003.88  hapax=77
  style: insufficient samples (1/3 references)

== sample3_economics.txt ==
  paragraphs=2  sentences=8  words=121
  TTR=0.7355  MTLD(0.720)=128.11  Honoré=2667.66  hapax=73
  style: insufficient samples (2/3 references)

== sample4_chatty_student.txt ==（口语化）
  paragraphs=2  sentences=10  words=134
  TTR=0.7761  MTLD(0.720)=167.59  Honoré=3638.40  hapax=90
  style refs=3 mean cosine=0.6506 max=0.6735

== sample5_formal_report.txt ==（正式书面）
  paragraphs=2  sentences=7  words=108
  TTR=0.7593  MTLD(0.720)=83.00  Honoré=3199.46  hapax=70
  style refs=4 mean cosine=0.7965 max=0.8672
```

可观察到预期现象：**正式书面报告与学术样本的风格余弦明显更高（0.87），
口语化随笔更低（0.67）**——但这只是“风格接近程度”的线索，不构成任何定性结论。
前 3 篇按顺序分析时参照样本不足 3 份，按规范返回 `insufficient_samples`，
第 4、5 篇才有风格分数（这是设计上的样本不足保护）。

---

## 8. 测试

测试覆盖需求点名的全部场景：

| 文件 | 覆盖 |
|---|---|
| `tests/test_dedup_upload.py` | 重复上传、批内重复、跨课程同文独立、归一化去重、100/10MB 限制与单文件拒收 |
| `tests/test_queue.py` | 并发领取顺序、generation 自增、心跳、**租约过期回收**、**失败重试到上限**、**僵尸 worker 不能完成/上报**、单文档失败不阻断整批 |
| `tests/test_mysql_concurrency.py` | **真实 MySQL 下 4 线程并发领取不重复**、过期任务只被回收一次（仅 MySQL 运行） |
| `tests/test_permissions.py` | **跨课程越权**读写 404/403、列表作用域、匿名 401、加入教师后可见 |
| `tests/test_pipeline_versions.py` | 端到端管线、**版本重跑**产生 v1/v2 且不改写旧行、重复重跑 409、批次进度 |
| `tests/test_metrics.py` | 段落/丰富度/重复/风格公式与边界、小样本标记、样本不足、免责声明 |
| `tests/test_docx.py` | DOCX（含表格）解析、损坏 DOCX 拒收且不影响同批 TXT |

### 用 SQLite 跑逻辑测试（无需数据库服务）
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m spacy download en_core_web_sm
python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab')"
USE_SQLITE=1 pytest -q
```

### 用真实 MySQL 跑（含 SKIP LOCKED 并发测试）
```bash
# 准备库（示例）
mysql -uroot -e "CREATE DATABASE textengine CHARACTER SET utf8mb4;
  CREATE USER 'textengine'@'%' IDENTIFIED BY 'textengine-pass';
  GRANT ALL ON textengine.* TO 'textengine'@'%';
  GRANT ALL ON test_textengine.* TO 'textengine'@'%';"

MYSQL_HOST=127.0.0.1 MYSQL_USER=textengine MYSQL_PASSWORD=textengine-pass \
  MYSQL_DATABASE=textengine pytest -q
```

实测：MySQL 8 上 **42 passed**（含 2 个并发领取测试）；
SQLite 上 40 passed / 2 skipped（SKIP LOCKED 用例仅在 MySQL 运行）。

---

## 9. 目录结构

```
analysis/
  models.py        # Course/Batch/Document/UploadAttempt/AnalysisTask/AnalysisResult
  extraction.py    # TXT/DOCX 提取、归一化、SHA-256
  nlp.py           # spaCy/NLTK 本地模型懒加载
  metrics.py       # 全部指标公式（ALGORITHM_VERSION = 1.0.0）
  queue.py         # 领取/心跳/阶段/完成/失败：行锁 + 代数围栏
  pipeline.py      # worker 处理管线与参照样本收集
  services.py      # 批量上传、去重、重跑入队
  views.py         # DRF API（全部按课程作用域）
  management/commands/
    run_worker.py  # 轮询 worker（可横向扩容）
    wait_for_db.py # 等待数据库/迁移就绪
    seed_demo.py   # 载入 samples/ 并打印真实指标
samples/           # 5 份固定样本文本
tests/             # 测试套件
textengine/        # Django 项目配置
scripts/download_offline_deps.sh
Dockerfile  docker-compose.yml  requirements*.txt
```

## 10. 安全与边界说明

- 鉴权：JWT（`rest_framework-simplejwt`）+ Session；默认全部接口需登录；
- 隔离边界是**课程**：只有课程 owner 或被加入的教师能读取该课程文本/结果；
- 文件内容只存归一化后的纯文本与摘要，DOCX 原件不落盘；
- 错误信息截断存储、不回传原始字节；异常堆栈仅写 worker 日志；
- 本系统是**线索工具**：任何数值（包括风格相似度高低、重复率高低）都不得被
  表述为“AI 生成”或“作弊成立”的结论。
