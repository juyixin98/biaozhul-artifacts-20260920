# SynapticGo

SynapticGo 是本地模型实验后端（Go + Echo + sqlx + PostgreSQL）。本期只支持
**单层线性分类器 + softmax**，聚焦三件事：

1. **数据集分块上传**——记录总大小、每块位置与 SHA-256；乱序可续传、重传幂等、
   内容冲突拒绝；缺块或整体摘要不符不能发布。
2. **模型登记与真实推理**——模型版本绑定数据集摘要、输入维度、权重和有序类别表，
   发布后不可变；推理在 CPU 上真实完成 float64 前向计算。
3. **实验记录与版本对比**——每次推理落库；只在类别表、输入维度、评测集摘要一致时
   比较指标。

没有媒体模块、没有复杂网络。

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

- API：`http://localhost:8080`
- PostgreSQL：`localhost:5432`（用户/密码/库名均为 `synapticgo`）
- 容器数据通过命名卷 `pgdata` 与 `blobdata` 持久化
- 迁移在进程启动时自动执行（SQL 已嵌入二进制）

另开终端跑端到端示例：

```bash
./examples/walkthrough.sh
```

> 若本机 5432/8080 已被占用，可用仓库附带的
> `docker-compose.smoke.yml`：把数据库端口限制在容器网络内、API 映射到
> 18091——
> `docker compose -f docker-compose.yml -f docker-compose.smoke.yml up --build`。

## 本地开发

需要 Go 1.25+ 与 PostgreSQL 14+。

```bash
# 建库（一次性）
sudo -u postgres psql -c "CREATE ROLE synapticgo LOGIN PASSWORD 'synapticgo' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE synapticgo OWNER synapticgo;"

# 运行（启动时自动迁移 + 崩溃恢复 + 后台 GC）
make run

# 测试（每个测试用例自动创建/删除独立的临时数据库）
make test
make test-race
```

可通过环境变量配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SYN_HTTP_ADDR` | `:8080` | 监听地址 |
| `SYN_DATABASE_URL` | `postgres://synapticgo:synapticgo@localhost:5432/synapticgo?sslmode=disable` | 数据库 DSN |
| `SYN_DATA_DIR` | `./data` | blob 与临时文件目录 |
| `SYN_MAX_CHUNK_BYTES` | `67108864` | 单块上传上限（64 MiB） |
| `SYN_TEST_DATABASE_URL` | 指向 `synapticgo_test` | 集成测试用连接串 |

## API 概览

所有 `/v1` 接口（除注册外）都需要：

```
Authorization: Bearer <sgk_...>
```

### 用户

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/users` | 注册，**只返回一次** bearer token |
| GET | `/v1/me` | 当前用户 |

### 数据集

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/datasets` | 声明清单：总大小、块大小、整体 SHA-256、每块 idx/size/sha256 |
| GET | `/v1/datasets` / `/v1/datasets/:id` | 列表 / 详情（含每块接收状态） |
| PUT | `/v1/datasets/:id/chunks/:idx` | 上传一块（`application/octet-stream`） |
| POST | `/v1/datasets/:id/publish` | 合并校验并发布 |
| GET | `/v1/datasets/:id/content` | 下载已发布内容 |
| DELETE | `/v1/datasets/:id` | 删除（有模型引用时 409） |

### 模型与推理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/models` | 登记不可变版本（权重、类别表、可选训练/评测集 id、指标） |
| GET | `/v1/models` / `/v1/models/:id` | 列表（`?name=` 过滤）/ 详情 |
| POST | `/v1/models/:id/predict` | 真实 CPU 前向 + softmax，落实验记录 |
| DELETE | `/v1/models/:id` | 删除版本（实验记录级联） |
| GET | `/v1/model-comparisons?a=&b=` | 版本指标对比 |
| GET | `/v1/experiments` | 实验记录（`?model_version_id=&before=&limit=`） |

数据集内容格式见 `examples/dataset.json`，权重登记格式见
`examples/weights.json`。

## 关键一致性设计

**分块上传**

- 上传时服务端重算每块 SHA-256 与大小：与清单一致才落盘引用；相同块重复
  PUT 是 200 幂等；同位置不同内容返回 409；大小不符返回 422。
- 块可以任意乱序到达，未收齐前可随时续传。
- 发布时服务端**重新串联合并重算整体 SHA-256**，缺块、未平铺或整体摘要不符
  一律 422，数据集不会进入 `ready`。

**合并与崩溃恢复**

- 数据集状态机：`uploading → publishing → ready`。只有 `ready` 可下载。
- 块字节先进入 `data/tmp`，再硬链接到内容寻址目录 `data/blobs/<ab>/<sha>`；
  合并在临时文件中完成、校验通过后才原子地把 `ready` 与 blob 引用一起提交。
- 进程在合并中断：重启时 `RecoverAtStartup` 清空临时目录，把卡在
  `publishing` 的行重置为 `uploading`，客户端无需重传任何块即可重新发布；
  半成品永远不会被标记为可用。

**去重、权限隔离与 GC**

- blob 按 SHA-256 内容寻址，相同块/相同数据集在所有用户间共享一个 inode；
  但所有数据集与模型访问都带 `owner_id` 过滤，他人资源一律表现为 404。
- `blobs.refcount` 由 PostgreSQL 触发器随 `dataset_chunks` / `datasets` 的
  引用增删自动维护，不会在应用层漂移。
- GC 用事务级 advisory lock 把「引用 + 硬链接」与「删行 + unlink」串行化：
  清理与新引用并发时，任何已提交引用指向的文件都不会被删掉；无引用文件在
  最后一个引用消失后才回收。

**模型不可变与推理**

- 权重按结构体字段序做规范化 JSON，对规范字节取 SHA-256；同 name+version
  重复登记 409，已发布版本没有任何更新接口。
- 登记时校验：权重形状（K 行 D 列、偏置 K 项）、类别表非空不重复、所有参数
  有限；绑定的数据集必须维度一致、标签不越界。
- 推理每次从数据库重新载入权重，计算
  `logits[k] = W[k]·x + b[k]`，再做平移稳定的 softmax；输入形状错误、
  NaN/Inf 拒绝（422/400），不存在任何固定预测捷径。

**版本对比**

- 仅当两侧 `classes_hash`（有序类别表）、`input_dim` 一致，且评测数据集
  内容摘要一致（或都缺省）才比较数值指标；否则 422，拒绝跨类别表的
  不可比比较。

## 目录结构

```
cmd/server/            进程入口（迁移、恢复、GC、HTTP 服务）
internal/
  api/                 Echo 路由与 handler
  auth/                Bearer token 认证（仅存 SHA-256 摘要）
  config/              环境变量配置
  database/            连接与嵌入式迁移
  dataset/             分块上传、合并发布、GC、崩溃恢复
  model/               模型登记、推理、实验记录、版本对比
  nn/                  线性层 + softmax CPU 前向计算
  spec/                数据集/权重格式、规范哈希、校验
  storage/             内容寻址 blob 存储与临时文件
  user/                用户注册
migrations/            SQL 迁移（随二进制嵌入）
examples/              样例数据集、权重与端到端脚本
*_test.go              集成测试（每用例独立临时数据库）
```

## 测试覆盖

`go test -race ./...` 覆盖：

- 乱序上传与续传、相同块重传幂等、内容冲突 409、大小不符 422；
- 缺块/整体摘要错误拒绝发布、未发布内容不可下载；
- 合并写入后注入故障 → 重启恢复 → 无重传成功再发布；
- 重复发布竞争（6 个数据集各并发双发布，最终内容完好）；
- 共享内容引用计数、删一个保留、删光才回收文件；
- GC 与 12 个用户并发发布持续交错，已发布数据不丢文件；
- 跨用户访问全部 404、列表隔离；
- 推理 softmax 数值与手算值逐项比对（1e-14 容差）、形状/非法值拒绝、
  实验记录分页、版本不可变；
- 不同类别表/不同评测集对比返回 422。
