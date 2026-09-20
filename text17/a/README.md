# SynapticGo

本地模型实验后端：数据集分块上传、模型登记与 CPU 推理。本期只支持**一层线性分类器 + softmax**，不含复杂网络与媒体模块。

技术栈：Go 1.22 · Echo v4 · sqlx · PostgreSQL 16 · Docker Compose。

## 快速启动

```bash
docker compose up --build
# 服务监听宿主机 :18080（容器内 :8080），数据库迁移与崩溃恢复在启动时自动完成
```

本地开发（需自备 PostgreSQL）：

```bash
export DATABASE_URL="postgres://postgres:postgres@localhost:5432/synapticgo?sslmode=disable"
go run ./cmd/server
```

端到端示例（标准库 Python，无需额外依赖）：

```bash
python3 examples/demo.py http://localhost:18080
```

## 认证与隔离

所有业务接口要求 `X-User-ID` 请求头标识所有者。一切资源按所有者隔离：
跨用户访问一律返回 **404**（而非 403），不泄露资源存在性。

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/datasets` | 创建数据集（声明 total_size、chunk_size、整体 SHA-256） |
| GET | `/datasets` / `/datasets/:id` | 列表 / 详情（含已传与缺失分块，用于续传） |
| PUT | `/datasets/:id/chunks/:index` | 上传分块，头 `X-Chunk-SHA256` 校验内容 |
| POST | `/datasets/:id/publish` | 合并发布（幂等） |
| DELETE | `/datasets/:id` | 删除（有引用则 409） |
| POST | `/models` | 创建模型 |
| GET | `/models` / `/models/:id` | 列表 / 详情（含版本） |
| DELETE | `/models/:id` | 删除（有实验引用则 409） |
| POST | `/models/:id/versions` | 登记版本（绑定数据集摘要、input_dim、labels、weights、bias，不可变） |
| GET | `/models/:id/versions/:version` | 版本详情 |
| POST | `/models/:id/versions/:version/predict` | 推理：`{"inputs": [[...], ...]}` |
| GET | `/models/:id/compare?versions=1,2` | 版本对比（类别表不同则标记不可比） |
| POST | `/experiments` | 记录实验（指标 JSON） |
| GET | `/experiments?model_id=&dataset_id=&model_version_id=` | 查询实验 |
| DELETE | `/experiments/:id` | 删除实验 |

## 设计要点

### 分块上传与发布

- 创建数据集时声明总大小、分块大小与整体 SHA-256；分块按索引上传，
  每块以 `X-Chunk-SHA256` 校验。**乱序可续传**：`GET /datasets/:id` 返回
  `uploaded_chunks` / `missing_chunks`。
- **幂等重传**：相同索引相同内容返回 200 不重复存储；相同索引不同内容返回 409。
- 发布时按序合并分块到 `tmp/merge-<id>.part`，逐块复核哈希，再校验整体摘要；
  缺块或摘要不符返回 409，数据集保持 `uploading`，不会把半文件标成可用。
- 合并、文件登记、状态更新在**单个数据库事务**中完成；进程中断时事务回滚，
  状态保持 `uploading`，残留的临时文件由下次启动时的恢复流程清理
  （重置 `merging` 状态、清空 tmp、扫描无数据库记录的孤儿文件与分块目录）。
- 并发发布在数据集行锁（`SELECT ... FOR UPDATE`）上串行化：先到者完成发布，
  后来者看到已提交状态并幂等返回成功。

### 内容寻址与引用回收

- 发布后的文件按内容摘要存于 `files/<sha256>`，相同内容的数据集共享同一文件。
- 删除数据集/模型前检查引用（模型版本、实验），有引用返回 409。
- 共享文件仅在最后一个引用消失后回收。**清理与新引用并发**通过对内容摘要的
  PostgreSQL 咨询锁（`pg_advisory_xact_lock`）串行化：发布方在锁内完成
  "提升临时文件 + 插入 files 行"，回收方在锁内重新检查引用并删除行，
  磁盘文件删除（`GCOrphanFile`）同样在锁内复核无行后才执行——
  因此不会删掉正在被新引用使用的文件。

### 模型与推理

- 模型版本绑定数据集摘要、输入维度、类别表与权重，**创建后不可变**（无更新接口）。
- 推理为真实 Go CPU 前向计算：`logits = x·Wᵀ + b`，数值稳定 softmax（减最大值）。
- 注册时校验权重/偏置形状与非法数值（NaN/Inf）；推理时校验输入张量形状与
  非法数值，拒绝返回 400，绝不返回固定预测。
- 版本对比：仅当两版本类别表完全一致时输出指标差值（`metric_diffs`），
  否则 `comparable=false` 且只返回原始数据，不比较不具可比性的指标。

## 测试

```bash
# 单元测试（推理数值正确性，无需数据库）
go test ./internal/...

# 集成测试（需要 PostgreSQL）
docker compose up -d db
TEST_DATABASE_URL="postgres://postgres:postgres@localhost:15432/synapticgo?sslmode=disable" \
  go test ./tests/ -v
```

集成测试覆盖：乱序上传与幂等重传、内容冲突拒绝、缺块/摘要错误拒绝发布、
并发发布、合并中断恢复、共享文件引用计数、清理与新引用竞争、
跨用户隔离、删除引用检查、推理数值正确性、版本对比可比性。

## 目录结构

```
cmd/server/          入口（连接、迁移、恢复、HTTP 服务）
internal/config/     环境变量配置
internal/store/      数据库连接与内嵌 SQL 迁移
internal/app/        业务逻辑：数据集/文件存储/引用回收/模型/实验
internal/inference/  线性分类器 CPU 前向计算
internal/httpapi/    Echo 路由与处理器
tests/               端到端集成测试
examples/demo.py     API 全流程示例
```
