# cdag — 内容驱动构建图（Content-Driven Build Graph）

纯后端、本地运行的增量构建服务。把一次构建描述成一张有向无环图（DAG），
每个节点声明命令、输入、输出、依赖、参数、读取的环境变量和依赖的命令行工具；
系统按**内容**（而非时间戳）计算缓存键，增量执行并复用未变化的节点，同时
对"命中 / 失效"给出逐条解释。

- 语言：Go 1.22，仅用标准库，无第三方依赖
- 运行方式：本地 CLI（`cdag build`）或本地 HTTP JSON 服务（`cdag serve`，默认仅监听 `127.0.0.1`）
- 只执行项目规格中**显式声明**的命令（`sh -c`），不联网、不连接任何云平台
- 缓存目录与工作目录严格分离（缓存放进工作目录会被直接拒绝）
- 无前端

## 目录结构

```
cmd/cdag/                CLI / HTTP 服务入口
internal/graph/          图：校验、环检测（DFS 着色）、稳定拓扑排序、目标闭包
internal/spec/           项目规格 JSON 模型与加载
internal/fingerprint/    内容指纹：SHA-256，覆盖输入内容/依赖/参数/环境/工具版本
internal/cache/          内容寻址缓存（原子发布）+ 每项目历史（失效解释基线）
internal/engine/         增量构建引擎与差异解释
internal/server/         HTTP JSON API
examples/                示例工程、请求样例、端到端验收脚本与实际运行日志
```

## 快速开始

```bash
go build ./...

# 校验
go run ./cmd/cdag validate --file examples/project.json

# 构建（缓存默认放在 project.json 旁的 .cdag-cache，永远在工作目录之外）
GREETING=hello MODE=plain go run ./cmd/cdag build --file examples/project.json

# 只构建某个目标（自动包含其传递依赖闭包）
go run ./cmd/cdag build --file examples/project.json --target package
```

## HTTP JSON 服务

```bash
go run ./cmd/cdag serve --cache-dir /tmp/cdag-cache --addr 127.0.0.1:8080
```

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| GET  | `/v1/projects` | 列出已注册项目 |
| POST | `/v1/projects` | 注册项目（JSON 即项目规格） |
| GET  | `/v1/projects/{id}` | 查看项目 |
| POST | `/v1/validate` | 只校验不注册（环检测会返回 cycle 路径） |
| POST | `/v1/builds` | 触发构建 `{"project_id":"...","targets":["..."]}`（targets 可省略=全部） |

可直接执行的 curl 样例见 [`examples/requests/`](examples/requests/)。

## 项目规格

```json
{
  "id": "demo",
  "workdir": "./workspace",
  "graph": {
    "nodes": [
      { "id": "source", "outputs": ["src/message.txt"] },
      {
        "id": "normalize",
        "command": "mkdir -p build && tr A-Z a-z < src/message.txt > build/normalized.txt",
        "inputs": ["src/message.txt"],
        "outputs": ["build/normalized.txt"],
        "depends_on": ["source"],
        "params": {"mode": "lower"},
        "env": ["GREETING"],
        "tools": [{"name": "tr", "probe": ["sh", "-c", "tr --version 2>&1 | head -n 1"]}]
      }
    ]
  }
}
```

- **`command`**：以 `sh -c` 在 `workdir` 内执行，stdin 为 `/dev/null`。
  省略 command 的节点是"源/聚合节点"，只校验其 outputs 文件存在，绝不执行任何命令。
- **`inputs` / `outputs`**：工作目录内的相对路径（禁止绝对路径与 `..` 逃逸），
  按**字节内容 SHA-256** 参与指纹；mtime 不参与。
- **`depends_on`**：依赖边。依赖节点的**指纹与输出文件内容**都进入本节点键，
  因此传递依赖的改动会准确传播。
- **`params`**：任意字符串参数，进入键。
- **`env`**：声明式环境变量白名单。只有列出的变量会（a）进入缓存键、
  （b）被传递给命令（命令环境里始终保留 `PATH`，其余一律不可见）。
- **`tools`**：用 `probe` 命令探测工具版本，版本串进入键。

## 缓存键包含什么

对每个节点，对以下规范化 JSON 求 SHA-256（键在 `NodeResult.key` 中返回）：

1. 每个输入文件的相对路径、权限位、**内容 SHA-256**（不含 mtime）
2. 每个依赖的节点 ID、依赖指纹、依赖全部输出文件的内容哈希
3. 排序后的 `params`
4. 排序后的声明环境变量名及其**当前值**
5. 每个工具的名称与探测版本

缓存布局：

```
<cache-root>/entries/<前2位>/<完整键>/meta.json   # 清单 + 完整指纹
<cache-root>/entries/<前2位>/<完整键>/out/<...>   # 输出文件
<cache-root>/history/<项目id>.json                # 每项目上次指纹（解释基线）
```

条目按内容寻址、跨项目共享；历史按项目隔离。条目用临时目录 + 原子 rename
发布，失败节点不会留下半成品。

## 节点状态与失效解释

构建报告每个节点带 `status` 与可读 `reason`；重建时 `changes` 逐条列出原因：

- `built`：执行了命令；`changes` 可能含 `input_changed`、`dependency_changed`、
  `param_changed`、`env_changed`、`tool_version_changed` 等
- `cached`：命中内容缓存并恢复输出；`reason` 说明命中依据
- `verified`：无命令的源节点，校验输出存在
- `failed`：命令失败或声明输入/输出缺失；**不发布缓存**，并记录失败历史
- `blocked`：上游失败，本节点跳过（报告说明是哪个依赖）

## 环检测

注册 / 校验 / 构建前进行 DFS 三色标记；发现环返回结构化错误，含闭合路径
（如 `[a b a]`）。环图不会执行任何命令。

## 测试

```bash
go test ./...            # 单元 + 集成（使用 t.TempDir，互不影响）
go test -race ./...      # 含竞态检测
bash examples/acceptance.sh   # 端到端验收（实际构建输出见 acceptance-output.log）
```

测试覆盖：冷构建全量执行、空构建全命中、**仅改时间戳不重建**、
改文件内容触发传递依赖精确重建且无关节点复用、参数变化、声明环境变量变化、
工具版本变化、未声明环境变量隔离、失败节点不发布缓存且阻塞下游、缺失输入、
环检测拒绝执行、目标闭包、缓存与工作目录分离。

## 安全边界

- 仅运行规格中显式给出的命令；服务本身不发起任何网络请求。
- 命令环境最小化（`PATH` + 声明的 env），未声明变量（如 `SECRET`）不会泄漏。
- 文件路径限制在工作目录内；缓存目录禁止位于工作目录内。
- HTTP 默认绑定回环地址，请求体限制 1 MiB。
