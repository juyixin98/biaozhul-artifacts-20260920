# cdbg — 内容驱动构建图（纯后端）

一个本地构建工程服务：把构建过程描述为 **DAG**（节点 = 一次工具调用），按
**内容**（而非时间戳）决定每个节点是否需要重新执行，未变化的节点直接从
内容寻址缓存恢复输出，并对每个节点解释“为什么命中 / 为什么失效”。

- 纯后端：Go 库 + CLI + 本地 JSON HTTP 接口，**无任何前端**。
- 仅运行构建图规格（fixture）中**显式声明的命令**，不执行网络下发的任意脚本。
- **不连接任何云平台**；缓存、状态与工作目录三者物理分离。
- 内容寻址：输入内容哈希、工具版本、渲染后参数、声明环境变量、依赖节点缓存键
  共同决定缓存键；**只改文件 mtime 不会触发重建**。

## 它解决的问题

传统 `make` 类工具以 mtime 判定增量，在以下场景会出错：`git checkout`、文件拷贝、
时钟漂移、CI 检出的源码时间戳一致却内容不同等。`cdbg` 只比较内容：

- 改动**传递依赖** → 沿 DAG 精确重建受影响链，无关节点全部复用缓存；
- 改动**参数** / **工具版本** / **声明环境变量** → 仅使用它们的节点失效；
- 改动**文件内容但保持时间戳** → 准确重建；只改时间戳、内容不变 → 全部命中。

## 目录结构

```
cmd/cdbg/                CLI：serve（HTTP 服务）/ run（进程内一次性构建）
internal/digest/         SHA-256 内容摘要（文件、目录树、规范 JSON）
internal/graph/          构建图模型、校验、可达性、拓扑排序与环检测
internal/cache/          内容寻址缓存（发布/恢复，失败不留半成品）
internal/runner/         命令执行（sh -c / 直接执行、环境白名单、超时）
internal/engine/         增量构建引擎：缓存键、调度、失效解释、状态持久化
internal/server/         JSON HTTP 接口
examples/                构建图规格与请求/响应样例
scripts/acceptance.sh    端到端验收脚本（实际执行夹具命令）
```

## 构建

需要 Go 1.22+，运行时依赖 `sh`（POSIX shell）、以及夹具自身用到的系统命令
（示例用了 `printf`、`cat`、`sha256sum`）。

```bash
go build -o cdbg ./cmd/cdbg
```

## 构建图规格（spec）

spec 是一份 JSON，由 `tools` 与 `nodes` 两部分组成。完整示例见
[`examples/spec.json`](examples/spec.json)。

### tool（工具）

| 字段 | 说明 |
|---|---|
| `name` | 工具名 |
| `tool_version` | **工具版本**；改变它会使所有使用该工具的节点缓存失效 |
| `command` | 命令。`shell:true` 时 `command[0]` 是一段 `sh -c` 脚本，节点参数以 `$1,$2,…` 传入；否则 `command` 是可执行文件及其固定参数 |
| `shell` | 是否经 `sh -c` 执行 |
| `declared_env` | 允许从父进程**继承**的环境变量白名单；其余变量不透传 |
| `env` | 工具固定环境变量（Key=Value），始终计入缓存键 |

### node（节点）

| 字段 | 说明 |
|---|---|
| `name` | 唯一名 |
| `tool` | 引用的工具 |
| `inputs` | 声明输入文件/目录（相对工作目录）；其**内容哈希**计入缓存键 |
| `deps` | 依赖的其它节点；依赖节点的缓存键计入本节点键（传递依赖自动覆盖） |
| `outputs` | 声明输出（相对路径，禁止 `..` 逃逸）；成功后被收入缓存 |
| `params` | 参数表；在 `args` 中以 `{{.key}}` 引用 |
| `args` | 命令参数，支持 Go `text/template` 语法；渲染后的最终值计入缓存键 |
| `timeout_sec` | 超时（默认 600s） |

### 缓存键构成

每个节点的键 = SHA-256(规范 JSON({
节点名, 工具名, **工具版本**, 工具命令, shell 模式,
**渲染后参数**, params, **生效环境变量**,
**输入文件/目录内容哈希**（含相对路径与权限，不含 mtime）,
**各依赖节点缓存键** }))。

依赖键嵌套意味着：任何传递依赖的内容变化都会沿 DAG 改变本节点键。

## CLI 用法

缓存与状态目录必须位于工作目录之外（默认 `~/.cdbg/cache`、`~/.cdbg/state`，
可用 `CDBG_HOME` 覆盖）。

```bash
# 一次性构建（最适合脚本/CI）
./cdbg run -spec examples/spec.json \
  -workdir /tmp/demo/work -cachedir /tmp/demo/cache -statedir /tmp/demo/state

./cdbg run -spec spec.json -targets hash_merged   # 只构建某目标及其依赖
./cdbg run -spec spec.json -dry-run               # 只规划+解释，不执行、不写缓存
./cdbg run -spec spec.json -force                 # 忽略缓存查找强制重建（仍发布）
```

退出码：成功 0；存在失败节点或规格/环错误时非 0。结果以 JSON 打印到 stdout。

## HTTP 接口

```bash
./cdbg serve -addr 127.0.0.1:8787 \
  -workdir /tmp/demo/work -cachedir /tmp/demo/cache -statedir /tmp/demo/state
```

默认只监听回环地址。请求体 8MiB 上限。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/api/v1/build` | 执行一次（增量）构建 |
| POST | `/api/v1/explain` | 等价于 `dry_run:true` 的 build，只解释命中/失效，不执行命令 |
| GET  | `/api/v1/cache/entries` | 列出缓存中全部条目元数据 |
| GET  | `/api/v1/cache/entries/<sha256>` | 查询单个缓存条目 |

请求字段：`work_dir`、`spec`（内联）或 `spec_file`（服务端本地路径）、
`targets`、`force`、`dry_run`。

```bash
curl -s -X POST http://127.0.0.1:8787/api/v1/build \
  -H 'Content-Type: application/json' \
  --data @examples/request-full.json | jq .
```

更多请求样例：[`examples/request-full.json`](examples/request-full.json)、
[`request-target.json`](examples/request-target.json)、
[`request-explain.json`](examples/request-explain.json)、
[`request-force.json`](examples/request-force.json)、
[`request-inline.json`](examples/request-inline.json)；
真实响应样例：[`response-first-build.json`](examples/response-first-build.json)、
[`response-incremental.json`](examples/response-incremental.json)。

### 节点状态语义

| status | 含义 |
|---|---|
| `built` | 未命中缓存，实际执行了命令并发布缓存 |
| `cached` | 命中缓存，输出已从缓存恢复 |
| `failed` | 命令非零退出 / 超时 / 成功但声明输出缺失；**不发布缓存** |
| `skipped` | 上游失败，本节点跳过 |
| `error` | 构建前错误（如声明输入缺失、无法启动命令） |

`reasons` 字段用中文逐条解释命中或失效原因，例如：

```
缓存失效：本次键 00fd518a5ca5… 与上次键 0cdf6f4d1ad0… 不同
依赖缓存键变化（传递依赖变更沿 DAG 传播）：gen_source 6944dd6a23b8… → 3f1c427b9721…
```

规格非法（未知工具/节点、路径逃逸、模板引用未声明参数等）和依赖环返回
`400`，响应带 `cycle`（首尾相接的环路径）；节点命令失败返回 `200` 且
`success:false`（这是执行结果而非请求错误）。

## 安全与边界

- 命令**只来自 spec**：服务端不提供“执行任意命令”的接口；`spec_file` 只读取
  服务端本地文件。
- 工作目录固定：所有命令 `cwd` = workdir；`inputs/outputs` 必须是相对路径且
  禁止 `..` 逃逸；输入树中出现符号链接直接报错。
- 环境变量最小化：仅 `declared_env` 白名单 + 工具固定 `env`，避免宿主机
  `PATH`/密钥等泄漏进缓存键与子进程。
- 缓存发布走临时目录 + 原子改名；失败自动清理，不存在半成品条目。
- 无网络客户端代码，无云依赖。

## 测试与验收

```bash
# Go 单元/集成测试（digest、graph、cache、runner、engine、HTTP server）
go test ./...
go vet ./...

# 端到端验收脚本：构建 CLI 后在隔离临时目录中实际运行夹具命令
./scripts/acceptance.sh
```

验收脚本覆盖 52 项断言，包括：

1. 首次全量构建 → 二次全缓存命中；
2. **仅修改 mtime**（内容不变）→ 全部命中；
3. **修改参数**（生成文件内容变化）、保持旧 mtime → 传递链精确重建、
   无关节点复用；
4. **修改输入文件内容但保持 mtime** → 精确失效并准确重建；
5. 工具版本变化 → 仅使用者重建；
6. 失败节点不发布缓存、下游 `skipped`，恢复后重新执行；
7. 依赖环检测（返回首尾相接的环）；
8. dry-run 不执行任何命令、不产生输出；
9. 缓存/状态目录与工作目录完全分离。

## 已知限制

- 缓存按输出原样保存，不做跨机器/跨平台重定位（纯本地方案）。
- 节点输出若包含未在 `outputs` 声明的文件，这部分不会被缓存（声明式契约）。
- 非确定性命令（每次运行输出不同）天然无法命中缓存，这是内容驱动的预期行为。
