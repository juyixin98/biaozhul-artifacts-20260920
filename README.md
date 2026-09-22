# 本地文本分析任务引擎（text27-b）

纯本地的学生作文统计分析引擎：Django REST Framework + MySQL + spaCy + NLTK，
**不连接任何外部模型服务或远程 API**。批量接收 TXT/DOCX，计算描写性统计指标
（段落长度、词汇丰富度、重复片段、与课程样本的风格相似度），由数据库队列驱动
多个 worker 进程并发处理。

> ⚠️ 所有输出都是**线索性统计指标**。系统不会、也不能输出“确定由 AI 生成”或
> 任何学术不端结论。每份结果都附带 `disclaimer` 与完整 `methodology`。

---

## 1. 目录结构

```
config/                 Django 项目配置（MySQL/SQLite 可切换）
engine/
  models.py             Course / Submission / AnalysisTask / AnalysisResult / StyleSample
  queue.py              DB 队列：原子领取、租约、fence token、重试退避、过期回收
  worker.py             工作进程：心跳续租 + 执行 + 防旧 worker 覆盖落库
  services.py           上传校验、内容摘要去重、批量入库、重跑
  analysis/
    extract.py          TXT(chardet)/DOCX(python-docx) 提取与规范化
    nlp.py              spaCy 标注（缺模型时正则退化并透明告警）
    metrics.py          段落/句长、TTR、MTLD、hapax、重复 n-gram、重复句
    style.py            风格特征向量 + 余弦相似度
    pipeline.py         EXTRACT→NLP→METRICS→STYLE→PERSIST 阶段编排
    version.py          算法版本 local-style-v1 与公式/口径说明
    sample_data.py      固定演示样本（7 份样本 + 1 份待分析文本）
  views.py / urls.py    REST API（教师仅能访问自己的课程）
  tests/                33 个测试：去重/并发/租约/重试/越权/版本/E2E
  management/commands/  run_worker / seed_demo / seed_samples / demo_analysis
scripts/vendor_offline.sh  离线 wheel + NLTK 数据打包
Dockerfile / Dockerfile.offline / docker-compose.yml
docs/demo_metrics_output.txt  固定样本跑出的真实指标（可复现）
```

## 2. 指标与公式（算法版本 `local-style-v1`）

所有公式同时随每份结果的 `methodology.formulas` 持久化，历史结果可复现。

| 指标 | 公式 / 口径 |
|---|---|
| **段落长度** | 以空行切分段落，统计每段词数的 min/max/mean/median/stdev 与 5 档直方图 |
| **句子长度** | spaCy 分句后每句词数的同样统计 |
| **TTR** | 类符数/形符数 `V/N`（小写、仅字母词、lemma） |
| **MTLD** | McCarthy & Jarvis (2010)：顺序累计词标，因子内 TTR≤0.72 记一个完整因子；因子数含末段按比例折算 `(1-TTR)/(1-0.72)`；`MTLD=N/因子数`，取正/逆向均值。值越高词汇越多样 |
| **hapax ratio** | 全文仅出现 1 次的词型数 / 词型总数 |
| **重复 5/10-gram 占比** | `Σ_gram (出现次数−1)·n / N`，即可被重复片段解释的词标比例 |
| **最长重复跨度** | 所有“重复 n-gram 起始位置”中最长连续段（词数），并给出原句示例供人工核对 |
| **重复句比例** | 规范化（小写+折叠空白）后多余出现的句子实例数 / 总句数 |
| **风格相似度** | 向量 = 30 个高频功能词相对频率 + 12 个词性粗类相对频率 + 6 个标量（均词长、均句长/10、TTR、MTLD/100、hapax、逗号/句），L2 归一化后两两**余弦相似度** |

**样本不足处理**

- 课程内可参照样本 < **3** 份（或样本全部与被分析文本内容摘要相同）时，
  `style_similarity` 为 `null`，返回 `insufficient_samples` 提示，**不猜测数值**。
- 与被分析文本 **SHA-256 摘要相同**（同文本自比）的样本自动排除，杜绝相似度=1 的自比假象。
- 全文 < **50 词**时照常返回数值，但追加 `low_word_count` 警告。
- 运行环境缺少 spaCy 模型时退化为内置正则分词，指标照常计算，结果标注
  `spacy_unavailable`（Docker 镜像内置模型，正常运行不会出现）。

**版本策略**：结果绑定 `input_hash`（规范化文本 SHA-256）+ `algorithm_version`。
重跑总是追加新 `version` 行，旧版本永久保留。

## 3. 队列与并发正确性

- **领取**：`SELECT … FOR UPDATE SKIP LOCKED` 取候选 + 条件置位（MySQL/InnoDB 行锁互斥），
  多 worker 不会领到同一任务。
- **租约**：领取时写 `lease_expires_at`（默认 60s），后台心跳线程每 1/3 租期续租。
- **崩溃回收**：worker 中断后租约过期，`reclaim_expired()` 把任务放回 PENDING，
  `attempts+1`；超过 `max_attempts`（默认 3）置 FAILED。
- **fence token**：每次领取/回收 `run_token+1`。旧 worker 恢复后，其心跳、失败上报、
  结果提交都因 `run_token` 不匹配被拒绝（影响行数 0），**无法覆盖新结果**。
- **失败处理**：单文档失败按 5、10、20s… 指数退避重试；错误保留 `stage`
  （EXTRACT/NLP/METRICS/STYLE/PERSIST）、`error_code`、`error_message`；
  单文件失败不阻断同批其他文件。

## 4. REST API（Token 认证；教师只能访问自己的课程，越权返回 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/token/` | 已登录会话获取 API Token |
| GET/POST | `/api/courses/` | 列出/创建课程（自动绑定教师） |
| GET/POST | `/api/courses/{cid}/samples/` | 风格样本（multipart `file` 或 JSON `text`，`label`∈student/reference/known_ai） |
| POST | `/api/courses/{cid}/submit-batch/` | multipart `files`（≤100 个，每个 ≤10MB，.txt/.docx） |
| GET | `/api/courses/{cid}/batches/{bid}/` | 批次进度（pending/running/succeeded/failed/overall_progress） |
| GET | `/api/courses/{cid}/tasks/{tid}/` | 单任务状态、阶段、尝试次数与错误 |
| GET | `/api/courses/{cid}/submissions/` | 提交列表 |
| GET/POST | `/api/courses/{cid}/submissions/{sid}/` | 详情+全部版本结果；POST = 重跑（生成新版本） |
| GET | `/api/courses/{cid}/submissions/{sid}/results/{v}/` | 指定历史版本 |

提交响应逐文件给出 `accepted / duplicate / rejected`（含可定位的 error_code）。
**去重键为 `(course, content_hash)`**：同课程同内容幂等返回既有提交（不新建任务）；
相同文本在不同课程中的提交、权限与结果完全独立。

## 5. 快速启动（Docker Compose，推荐）

```bash
docker compose up --build
# web:      http://localhost:18027  （宿主 8000 被占用时映射到 18027，见 compose）
# worker:   2 个副本并发消费；MySQL 8 带健康检查
# 演示账号:  demo_teacher / demo1234（worker 启动时自动 seed_demo）
# Compose 项目名与镜像名固定为 text27b-engine，避免与同名目录项目冲突
```

> 端口/子网可在 `docker-compose.yml` 调整（默认子网 10.77.27.0/24 用于规避
> 宿主默认地址池耗尽）。

手动初始化演示数据 / 跑固定样本指标：

```bash
docker compose exec web python manage.py seed_demo
docker compose exec web python manage.py demo_analysis   # 打印真实指标 JSON
```

## 6. 本地开发（无需 Docker，SQLite）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python -m nltk.downloader -d offline/nltk_data stopwords punkt_tab punkt

DB_ENGINE=sqlite python manage.py migrate
DB_ENGINE=sqlite python manage.py demo_analysis     # 端到端演示
DB_ENGINE=sqlite python manage.py run_worker        # 另开终端启动 worker
DB_ENGINE=sqlite python manage.py test engine       # 运行 33 个测试
```

对接本地/远程 MySQL 只需设置环境变量（默认即 mysql）：
`MYSQL_HOST / MYSQL_PORT / MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD`。

## 7. 离线环境准备（内网部署）

在**联网机器**上执行（与目标部署同平台、同 Python 版本）：

```bash
bash scripts/vendor_offline.sh
```

产出 `offline/wheels/`（全部依赖 + `en_core_web_sm` 模型 wheel）、
`offline/requirements.lock`、`offline/nltk_data/`（停用词/punkt）。
整个目录拷贝到内网后：

```bash
pip install --no-index --find-links offline/wheels/ offline/wheels/*.whl
docker build -f Dockerfile.offline -t text-engine:offline .
```

## 8. 固定样本的真实指标（`python manage.py demo_analysis`）

待分析文本是一篇学生口吻图书馆短文，**刻意植入两处整句复制粘贴**。
spaCy 后端真实输出（完整 JSON 见 `docs/demo_metrics_output.txt`）：

```json
"lexical_diversity": {"word_count": 159, "ttr": 0.6226, "mtld": 73.3, "hapax_legomena_ratio": 0.6869},
"repeated_fragments": {"repeated_5gram_share": 0.4717, "repeated_10gram_share": 0.3145,
                       "longest_repeat_span_words": 12,
                       "examples": ["i learned that octopuses can change both color and texture very quickly", ...]},
"repeated_sentences": {"repeated_sentence_ratio": 0.1538},
"style_similarity": {"nearest": {"name": "student-01-my-hometown.txt", "label": "student", "similarity": 0.9958},
                     "mean_by_label": {"student": 0.9935, "reference": 0.9846}}
```

解读：重复指标准确标出两处粘贴（近 47% 的词标处于重复 5-gram 中）；
风格上与学生样本的统计距离近于参考范文样本——但这只反映**表层统计风格的接近程度**，
受作者、题材、模板共同影响，不是来源判定。

## 9. 测试清单（33 个，SQLite 与 MySQL 8 均通过）

- 共 33 个测试。`test_uploads`：重复上传去重、跨课程独立、坏 DOCX 不阻断整批、100 份/10MB 边界
- `test_queue`：并发领取唯一、租约过期回收、**旧 worker 无法覆盖**、退避重试、上限 FAILED
- `test_authorization`：跨课程读/写/样本/结果越权 404、匿名 401、列表按教师隔离
- `test_versions`：重跑生成 v1/v2、输入摘要变化绑定新 hash、历史版本保留
- `test_metrics_style`：TTR/hapax/MTLD 公式、重复片段检出与清洁文本、样本不足返回 null、两语体可分
- `test_end_to_end`：API 全链路、批次进度、自比样本排除、免责声明/方法论存在

另外在真实 MySQL 8 上用 3 个并发 worker 消费 50 个任务做过压测：50 任务 →
50 份结果，每个 submission 恰好 1 份，无重复提交。
