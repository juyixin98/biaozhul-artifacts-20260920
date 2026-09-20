# SynapticGo

本地模型实验后端：数据集分块上传、模型登记与真实 CPU 推理。本期只支持
**一层线性分类器 + softmax**（多项逻辑回归），不含复杂网络与媒体模块。

技术栈：Go 1.22 · Echo v4 · sqlx · PostgreSQL 16 · Docker Compose。

## 快速启动

```bash
docker compose up --build
# API:    http://localhost:18081 （容器内 :8080）
# 数据库: 宿主机 localhost:15433
# 启动时自动执行迁移与崩溃恢复
```

端口可用环境变量覆盖：`APP_PORT`、`DB_PORT`。

本地开发（需自备 PostgreSQL）：

```bash
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/synapticgo?sslmode=disable"
export DATA_DIR=./data
go run ./cmd/server
```

端到端示例（只用 Python 标准库）：

```bash
python3 examples/demo.py http://localhost:18081
```

## 认证与隔离

所有业务接口都要求 `X-User-ID` 请求头标识所有者（缺失返回 401）。
资源全部按所有者隔离：跨用户访问一律 **404**（不是 403），不泄露资源是否存在。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/datasets` | 创建数据集，声明 `total_size` / `chunk_size` / 整体 `sha256` |
| GET | `/datasets` | 列出本人数据集 |
| GET | `/datasets/:id` | 详情，含 `uploaded_chunks` / `missing_chunks`（用于续传） |
| PUT | `/datasets/:id/chunks/:index` | 上传分块（裸字节），头 `X-Chunk-SHA256` |
| POST | `/datasets/:id/publish` | 合并发布（幂等） |
| DELETE | `/datasets/:id` | 删除（存在引用时 409） |
| POST | `/models` | 创建模型 |
| GET | `/models` / `/models/:id` | 列表 / 详情（含全部版本） |
| DELETE | `/models/:id` | 删除（有实验引用时 409） |
| POST | `/models/:id/versions` | 登记不可变版本 |
| GET | `/models/:id/versions/:version` | 版本详情 |
| POST | `/models/:id/versions/:version/predict` | 推理 `{"inputs": [[…], …]}` |
| GET | `/models/:id/compare?versions=1,2` | 版本对比（类别表不同则不可比） |
| POST | `/experiments` | 记录实验（`metrics` 为 JSON 对象） |
| GET | `/experiments?model_id=&model_version_id=&dataset_id=` | 查询实验 |
| DELETE | `/experiments/:id` | 删除实验 |
| GET | `/healthz` | 健康检查 |

## 关键设计

### 分块上传与发布

- 客户端创建数据集时声明总大小、固定分块大小与整体 SHA-256；分块可
  **乱序、可续传**。每块除 `X-Chunk-SHA256` 头校验外，还按索引校验精确
  字节数（除最后一块外每块必须等于 `chunk_size`）。
- **幂等重传**：同索引同内容返回 200 且不重复落盘；同索引不同内容 409；
  头与内容不符 400。
- 发布时在持有数据集行锁的事务内把分块顺序合并到
  `tmp/merge-<id>.part`，逐块复核大小与摘要，并计算整体摘要。缺块、索引
  不连续或整体摘要不符 → 409，数据集保持 `uploading`，tmp 文件立即删除。
- 合并、内容文件登记、状态翻转为 `published` 在**同一事务**提交。进程在
  提交前崩溃 → 事务回滚（状态仍是 `uploading`），半文件绝不会被标成可用；
  残留 tmp 由启动时的 `Recover` 清理，同时把 `merging` 重置为 `uploading`、
  清扫无数据库记录的孤儿内容文件与孤儿分块目录。
- 并发发布在 `SELECT … FOR UPDATE` 数据集行锁上串行：后到者看到已提交的
  `published` 状态并幂等返回 200。

### 内容寻址与引用回收

- 发布后的文件按整体摘要存于 `files/<sha256>`；相同内容的多个数据集共享
  同一 `files` 行与同一磁盘文件。
- 删除数据集前检查 `model_versions` 与 `experiments` 引用（有则 409）；
  删除模型前检查实验引用（有则 409）。
- 共享文件只在最后一个引用消失后回收。**清理与新引用并发**时，发布方与
  回收方都在按内容摘要派生的事务级咨询锁（`pg_advisory_xact_lock`）内操作：
  发布方在锁内把私有 tmp 提升为正式文件并插入 `files` 行；引用释放方在锁内
  复查引用数并删行；磁盘删除（`GCOrphanFile`）另开短事务，在同一锁内复查
  行不存在才 unlink。因此正在被新引用使用的文件不会被删掉。

### 模型与推理

- 模型版本绑定创建时的数据集摘要（`dataset_sha256` 冗余落库）、输入维度、
  类别表、权重与偏置；**发布后不可变**（没有任何更新路径），版本号在模型
  行锁下单调分配。
- 注册时即做参数校验：类别非空且不重复、权重行数=类别数、列数=`input_dim`、
  偏置长度=类别数、所有权重/偏置必须有限（拒绝 NaN/Inf）。
- 推理是真实的 Go CPU 前向计算：`logits_j = b_j + Σ_i x_i·W[j][i]`，
  再做减最大值的数值稳定 softmax。逐行校验输入宽度与有限性，违规返回 400；
  结果完全由存储权重算出，不存在固定预测。

### 版本对比与实验

- 实验记录绑定「本人的模型版本 + 本人的已发布数据集」，`metrics` 必须是
  JSON 对象，可按模型 / 版本 / 数据集过滤。
- 对比返回两个版本各自的实验与数值型指标均值；**仅当类别表（含顺序）完全
  一致**才计算同键指标差值（v2−v1），否则 `comparable=false`、给出原因、
  不输出 `metric_diffs`，不比较不具可比性的指标。

## 目录

```
cmd/server/          入口：连接、迁移、崩溃恢复、HTTP 服务
internal/config/     环境变量配置（ADDR / DATABASE_URL / DATA_DIR）
internal/store/      数据库连接与内嵌 SQL 迁移（schema_migrations）
internal/app/        业务：数据集、文件存储、引用回收、模型、实验
internal/inference/  线性分类器 CPU 前向计算（可独立单测）
internal/httpapi/    Echo 路由、X-User-ID 鉴权、错误映射
tests/               针对真实 PostgreSQL 的端到端集成测试
examples/demo.py     API 全流程示例
```

## 测试

```bash
# 推理数值正确性等单元测试（无需数据库）
go test ./internal/...

# 端到端集成测试（需要 PostgreSQL）
docker compose up -d db
TEST_DATABASE_URL="postgres://postgres:postgres@localhost:15433/synapticgo?sslmode=disable" \
  go test ./tests/ -v -race
```

集成测试覆盖：乱序上传 / 幂等重传 / 续传视图、内容冲突与摘要不符拒绝、
缺块拒绝发布、并发发布、合并中断后的恢复、共享文件引用计数、
清理与新引用竞争（多轮，`-race`）、删除引用检查、跨用户隔离（含列表与
401）、推理数值正确性与非法输入、版本参数校验、版本对比可比性、实验过滤。
