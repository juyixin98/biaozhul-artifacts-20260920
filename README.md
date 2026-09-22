# VideoForge

把**文本分镜**和**本地素材**合成为 MP4 的后端服务。不调用任何外部生成服务：
匹配只读本地素材库，渲染只跑本机 FFmpeg。

- **ASP.NET Core 8** minimal API · **Dapper** · **PostgreSQL 16** · **FFmpeg 6** · **Docker Compose**

## 功能

| 领域 | 说明 |
|---|---|
| 分镜约束 | 目标时长 5–60 秒；最多 20 个场景；每段描述 ≤500 字；`cut` 直切或 `fade` 淡入淡出 |
| 素材匹配 | 场景关键词 ↔ 素材标签（大小写不敏感），得分 = 命中关键词数；同分时按 `created_at, id` **固定排序** |
| 缺失反馈 | 匹配缺失时逐场景返回具体原因（无关键词 / 没有任何素材携带 `[xxx, yyy]`），提交前、提交时都可拦截 |
| API | 素材导入、项目创建、提交生成、状态查询、取消、尝试历史、成片下载，OpenAPI `/swagger` |
| 上传安全 | 按**文件魔数 + ffprobe** 校验真实类型（改名的 exe/截断文件一律拒绝）；单素材硬上限 50 MB；文件名只用于展示、落盘名由服务端生成，杜绝路径穿越 |
| 并发 | 最多 **3** 个任务并行渲染（信号量）；队列领取使用 `FOR UPDATE SKIP LOCKED`，多 worker/多副本不争抢 |
| 幂等 | 每个提交键唯一，同一 `submissionKey` 重放返回**原任务**，绝不产生重复任务（数据库唯一索引兜底） |
| 状态机 | `queued → processing → completed / failed / cancelled`；取消与完成并发时由条件 UPDATE 保证**只有一个终态落库** |
| 中断恢复 | worker 用心跳续约；进程崩溃 / kill -9 后，启动恢复与定时 reaper 把陈旧任务回收重排，旧尝试标记 `interrupted` |
| 原子发布 | 先写临时文件 → ffprobe 独立校验（存在性、大小、时长容差）→ 原子 rename 到**尝试专属**发布路径 → 条件置 completed；残缺文件永远不会成为成片，被回收的陈旧 worker 也无法覆盖更新的成片 |
| 可追溯 | 每次尝试单独记录结果、错误、FFmpeg 日志尾部、**素材版本快照（含 SHA-256）**；重试不会覆盖已发布成片 |

## 快速开始（Docker Compose）

前置：Docker + Docker Compose。

```bash
docker compose up --build
```

- API： http://localhost:8080
- Swagger： http://localhost:8080/swagger
- PostgreSQL： 容器内 5432，宿主机映射到 **5433**（避让本机已占用的 5432；用户/库/密码均为 `videoforge`，仅演示用）

首次启动自动建表。生成样例素材并跑端到端示例（需要本机 `ffmpeg`、`curl`、`jq`）：

```bash
scripts/generate-samples.sh            # 生成到 ./sample-assets（纯本地 ffmpeg）
scripts/e2e-demo.sh                    # 导入→建项→提交→轮询→下载→ffprobe 校验
```

成片保存为 `./result-<jobId>.mp4`。

## 本地裸机运行（开发）

需要 .NET 8 SDK、PostgreSQL、ffmpeg/ffprobe。

```bash
# 1. 起一个本地 Postgres（或用 compose 只起 db）
docker compose up -d db

# 2. 运行（默认连接 localhost:5432）
dotnet run --project src/VideoForge.Api
```

可用环境变量覆盖配置（前缀 `VideoForge__`）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `ConnectionStrings__Postgres` | localhost:5432 videoforge | 数据库连接串 |
| `VideoForge__DataDirectory` | `/var/lib/videoforge` | assets/tmp/published 根目录 |
| `VideoForge__MaxMaterialBytes` | 52428800 (50 MB) | 单素材大小上限 |
| `VideoForge__MaxParallelJobs` | 3 | 最大并行渲染数 |
| `VideoForge__StaleJobSeconds` | 45 | 心跳多久无更新判定为 worker 死亡 |
| `VideoForge__MaxAttempts` | 3 | 每任务最大尝试次数（首次 + 重试） |
| `VideoForge__PollIntervalMs` | 300 | 队列轮询间隔 |

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/materials` | multipart 上传：字段 `file` + `tags`（逗号分隔） |
| GET | `/api/materials` / `/{id}` | 素材列表/详情 |
| POST | `/api/projects` | 创建项目（见下方请求体） |
| GET | `/api/projects/{id}` | 项目详情 |
| GET | `/api/projects/{id}/match-report` | 匹配预演：每个场景的素材、得分、时长、缺失原因 |
| POST | `/api/projects/{id}/jobs` | 提交生成，body `{"submissionKey":"...", "priority":0}` |
| GET | `/api/jobs/{id}` | 状态查询 |
| POST | `/api/jobs/{id}/cancel` | 请求取消（排队中立即取消，渲染中协作取消） |
| GET | `/api/jobs/{id}/attempts` | 每次尝试的结果、错误、ffmpeg 日志、素材版本清单 |
| GET | `/api/jobs/{id}/download` | 下载成片（仅 completed，支持 Range） |

项目请求体：

```json
{
  "name": "demo-reel",
  "targetDurationMs": 8000,
  "transition": "fade",
  "scenes": [
    { "description": "Opening over the ocean", "keywords": ["ocean"] },
    { "description": "Closing at sunset", "keywords": ["sunset"] }
  ]
}
```

时长在场景间均分（余数分配给最前的场景，总和严格等于目标时长）。任务完成后
`outputDurationMs` 与下载文件的 ffprobe 时长应落在 ±600 ms 容差内。

## 关键设计：取消与完成的终态竞争

取消与渲染完成可能同时发生。两条终态转移都是带条件的单条 UPDATE：

- `completed` 仅当 `status='processing' AND locked_by=自己 AND cancel_requested=false`
- `cancelled` 仅当 `status='processing' AND locked_by=自己 AND cancel_requested=true`

二者互斥，数据库行锁保证**最多一条生效**。已 completed 的任务拒绝任何后续取消；
取消先到时，完成转移返回 0 行，worker 删除刚发布的文件并落 cancelled。

## 关键设计：中断恢复

- 渲染期间每 `StaleJobSeconds/5` 秒写一次心跳（远小于判定阈值，避免误杀）。
- 启动时无条件回收一次所有 `processing` 行（新进程不可能持有旧锁），之后定时 reaper 回收心跳陈旧的行。
- 崩溃时任务的临时文件留在 `tmp/`，启动时清理 10 分钟前的残留；任务重试用带尝试号的独立临时文件名。
- 恢复后重新领取会开启新的 attempt 行，旧尝试记为 `interrupted`，不影响成片只在全部尝试耗尽后才 `failed`。

## 测试

```bash
dotnet test
```

测试使用 **Testcontainers** 自动启动 `postgres:16-alpine`（需要能访问 Docker），
渲染类测试调用本机真实 `ffmpeg` 并用 `ffprobe` 校验产出。共 55 个测试，覆盖：

- 匹配得分、大小写、同分固定排序、缺失原因、时长分配
- 请求校验（时长/场景数/描述长度/提交键字符集）
- **队列争抢**：8 worker × 10 任务，每个任务恰好被领取一次；优先级/FIFO 顺序
- **重复提交**：12 个并发相同提交键只建一条任务；重放返回原任务
- **取消竞态**：排队取消、领取后取消、完成后取消、20 轮完成/取消对撞，终态唯一
- **进程中断恢复**：陈旧心跳回收、新鲜心跳不回收、重试到上限失败、取消优先、逐尝试日志
- 素材异常：exe 改名 png、文本伪装图片、路径穿越文件名、50 MB 超限、无标签
- **端到端真实渲染**：导入→建项→提交→完成→下载，ffprobe 实测时长（目标 6000 ms / 实测约 6021 ms）
- 损坏素材：每次尝试失败、3 次后 failed、无残缺文件可下载
- worker 并发：观测并行度 ≤ 3；渲染中实时取消且不发布文件；kill -9 后重启恢复并完成
