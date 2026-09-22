# VideoForge

把文本分镜和本地素材合成为 MP4。ASP.NET Core 8 + Dapper + PostgreSQL + FFmpeg，不调用任何外部生成服务。

## 功能

- **项目**：目标时长 5–60 秒，最多 20 个场景，每段描述 ≤500 字，转场可选 `fade`（淡入淡出）或 `cut`（直接切换）。
- **素材匹配**：按场景描述与素材标签做关键词匹配（大小写不敏感的包含计分）；同分按素材 id 升序的固定顺序裁决；无匹配时任务失败并给出具体场景、关键词和可用标签。
- **素材导入**：按魔数嗅探真实文件类型（png/jpeg/gif/mp4/webm），单素材上限 50MB，存储路径由服务端生成，杜绝路径穿越。
- **任务队列**：最多 3 个并行渲染（`SELECT ... FOR UPDATE SKIP LOCKED` 争抢）；同一幂等键只生成一个任务；状态机 `queued → processing → completed/failed/cancelled`。
- **取消竞态**：所有状态迁移都是条件 UPDATE（`WHERE status=... AND attempt=...`），取消与完成并发时只有一个终态能落库。
- **崩溃恢复**：启动时把中断的 `processing` 任务重新排队、清理临时文件；`completed` 但成片缺失的任务同样重新排队——残缺文件永远不会被标记为成片。
- **发布安全**：先渲染到临时文件，ffprobe 校验时长（容差 0.75s）后才原子 rename 发布；每次尝试的日志、错误、素材版本快照都存入 `job_attempts`；已发布成片不会被重试覆盖。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# API: http://localhost:8080  (PostgreSQL: localhost:5432)
```

### 本地开发

```bash
# 需要本地 PostgreSQL（先建库建角色）：
sudo -u postgres psql -c "CREATE ROLE videoforge LOGIN PASSWORD 'videoforge'"
sudo -u postgres createdb -O videoforge videoforge_app

ConnectionStrings__Postgres="Host=localhost;Port=5432;Username=videoforge;Password=videoforge;Database=videoforge_app" \
  dotnet run --project src/VideoForge.Api
# 默认 http://localhost:5047，可用 ASPNETCORE_URLS 覆盖；Swagger 在 Development 环境的 /swagger
```

表结构由应用启动时自动创建（`Schema.EnsureCreatedAsync`，带 15 次重试等待数据库就绪）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/assets` | 导入素材（multipart：`file`、`name`、`tags` 逗号分隔） |
| GET | `/api/assets` | 素材列表 |
| POST | `/api/projects` | 创建项目（`name`、`targetDurationSeconds`、`transition`、`scenes[].description`） |
| GET | `/api/projects/{id}` | 项目详情 |
| POST | `/api/projects/{id}/jobs` | 提交生成（`idempotencyKey`；重复键返回已有任务） |
| GET | `/api/jobs/{id}` | 状态查询（含每次尝试的日志/错误/素材版本） |
| POST | `/api/jobs/{id}/cancel` | 取消（已终态返回 409） |
| GET | `/api/jobs/{id}/download` | 下载成片 MP4 |

## 测试

```bash
# 需要测试数据库：
sudo -u postgres createdb -O videoforge videoforge_test
# 或用环境变量覆盖连接串：VIDEOFORGE_TEST_CONN="Host=...;Database=..."

dotnet test
```

49 个测试覆盖：队列争抢（并发上限与重复认领）、重复提交（20 路并发同键）、取消/完成竞态（25 轮随机时序）、进程中断恢复（重排队 + 临时文件清理 + 缺失成片回收）、素材异常（无匹配、文件丢失、渲染失败重试、已发布成片不被覆盖）、文件类型/路径穿越校验、API 校验，以及真实 ffmpeg 渲染并验证时长的集成测试。

## 端到端示例

```bash
# 服务运行后（默认 http://localhost:8080，可用 BASE 覆盖）：
BASE=http://localhost:8080 ./samples/e2e.sh
```

脚本会用 ffmpeg 自行生成样例素材（`samples/assets/`），导入、建项目、提交任务、轮询状态、下载成片并用 ffprobe 校验时长（目标 12s，容差 ±0.75s）。

## 结构

```
src/VideoForge.Api/
  Controllers/     Assets / Projects / Jobs
  Data/            Db + Schema（建表 DDL）、Dapper 仓储（条件 UPDATE 状态机）
  Services/        AssetMatcher（关键词匹配）、RenderPlanBuilder（ffmpeg 参数）、
                   FFmpegRenderer、JobProcessor（渲染→校验→原子发布）、
                   RenderWorker（并行争抢）、JobRecovery（启动恢复）
tests/VideoForge.Tests/
samples/           样例素材生成 + 端到端脚本
Dockerfile / docker-compose.yml
```
