# depscan — 增量依赖扫描服务

一个**纯本地**的构建工程后端服务（Go 实现，无前端）：扫描 C 风格源码树的
`#include` 依赖，维护稳定的依赖图，并通过 JSON HTTP 接口回答
"改了/删了哪些文件，会影响哪些文件和构建目标"。

- **纯本地**：只绑定回环地址，不连接任何云平台，不执行任何外部命令。
- **缓存与工作目录分离**：扫描结果缓存在独立的 `-cache-dir` 中；若缓存目录
  位于被扫描源码根内（或反之），请求会被拒绝（HTTP 400）。
- **C 风格 include 子集扫描器**：
  - `#include "x"` —— 先搜索包含文件所在目录，再按顺序搜索 `includeDirs`；
  - `#include <x>` —— 只按顺序搜索 `includeDirs`；
  - 注释（`//` 与 `/* */`，含跨行块注释）中的伪 include 一律忽略；
  - **宏生成 include（如 `#include MACRO`）明确不支持，直接报错**（HTTP 422）。
- **循环引用**：不会死循环；循环以规范化形式（从字典序最小节点开始）报告在
  `cycles` 字段。
- **稳定输出**：所有文件列表、依赖列表、循环、受影响集合均排序，同一输入
  必得同一输出。

## 构建与运行

```sh
go build -o bin/depscan ./cmd/depscan
./bin/depscan -addr 127.0.0.1:8080 -cache-dir /tmp/depscan-cache
```

`-cache-dir` 默认为用户缓存目录下的 `depscan/`（如 `~/.cache/depscan`）。

## JSON 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/v1/scan` | 全量扫描，重建依赖图并写缓存 |
| POST | `/v1/scan/incremental` | 增量扫描：按显式给出的 `changed`/`deleted` 更新缓存图 |
| GET  | `/v1/graph?sourceRoot=...` | 读取缓存中的依赖图 |

### 请求体（`/v1/scan`）

```json
{
  "sourceRoot": "/abs/path/to/project",
  "includeDirs": ["include", "."],
  "targets": [
    { "name": "app", "sources": ["main.c"] },
    { "name": "net", "sources": ["net/open.c"] }
  ]
}
```

`/v1/scan/incremental` 在此基础上增加：

```json
{ "changed": ["util.h"], "deleted": ["svc/b.h"] }
```

`changed`/`deleted` 由调用方**显式提供**（服务不自行监听文件系统、不运行
任何外部命令）；`changed` 中已不存在的文件按删除处理。

### 响应

```json
{
  "graph":           { "main.c": ["include/util.h", "util.h"], "...": [] },
  "cycles":          [["svc/a.h", "svc/b.h"]],
  "unresolved":      [{"file": "a.c", "include": "missing.h", "angle": true, "line": 2}],
  "affectedFiles":   ["main.c", "util.h"],
  "affectedTargets": ["app"],
  "filesScanned":    8,
  "cachePath":       "/tmp/depscan-cache/scan-460e....json"
}
```

- `graph`：文件 → 排序后的直接依赖（均为相对源码根的斜杠路径）。
- `affectedFiles`：变更/删除文件的**反向传递闭包**（谁直接或间接包含了它们）。
  删除文件时，闭包基于删除**前**的旧图计算，因此被删文件的依赖方仍会被正确标记。
- `affectedTargets`：`sources` 与受影响文件相交的目标名。

### 错误码

| 状态码 | 场景 |
|---|---|
| 400 | 配置错误（缺 `sourceRoot`、缓存目录与源码根未分离等） |
| 404 | `GET /v1/graph` 无缓存 |
| 409 | 未做全量扫描就请求增量扫描 |
| 422 | 宏生成 include（`#include MACRO`）——明确不支持 |
| 500 | 其他内部错误 |

## 设计要点

- **同名头文件**：解析顺序即语义——引号 include 先查包含文件所在目录，
  再查 `includeDirs`；尖括号 include 只查 `includeDirs`。首个命中者胜出，
  因此 `main.c` 的 `"util.h"` 解析到根目录的 `util.h`，而 `net/open.c` 的
  `"util.h"` 解析到 `net/util.h`。
- **注释剥离**：扫描前用状态机将 `//`、`/* */` 注释内容替换为空白（保留换行，
  行号不变），字符串字面量原样保留，故注释与字符串中的伪 include 不会
  进入依赖图，也不会出现在 `unresolved` 中。
- **循环处理**：图遍历用灰/黑标记避免无限递归；DFS 回边提取循环并去重、
  规范化后输出。
- **缓存**：每个源码根对应一个 `scan-<sha256(root)>.json`，存于独立缓存目录。

## 自动化测试

```sh
go vet ./...
go test ./...
```

测试覆盖（`internal/*/`）：

- 同名头文件（引号 vs 尖括号解析顺序）—— `service_test.go`
- 嵌套路径（`"lib/api/client.h"`、`"../util/buf.h"`）—— `service_test.go`
- 注释/字符串中的伪 include —— `scanner_test.go`、`service_test.go`
- 循环引用（不挂死、规范化输出）—— `graph_test.go`、`service_test.go`
- 宏生成 include 报错（含行号）—— `scanner_test.go`、`service_test.go`、`server_test.go`
- 文件删除（节点与入边移除、受影响目标）—— `service_test.go`
- 缓存分离强制、未解析 include 报告、增量前置条件 —— `service_test.go`
- HTTP 端到端（全量→增量→读缓存图、422/400）—— `server_test.go`

## 实际运行记录（2026-09-24，本机 go1.22.2 linux/amd64）

以下命令均真实执行过，结果如实记录：

| 命令 | 结果 |
|---|---|
| `gofmt -l .` | 通过（无输出） |
| `go vet ./...` | 通过 |
| `go build ./...` | 通过 |
| `go test ./...` | **全部通过**：`ok depscan/internal/api`、`ok depscan/internal/graph`、`ok depscan/internal/scanner`、`ok depscan/internal/service`（`cmd/depscan` 无测试文件） |
| `curl POST /v1/scan`（demo 工程） | 200；8 文件；同名头解析正确；`cycles: [["svc/a.h","svc/b.h"]]`；`unresolved: []` |
| `curl POST /v1/scan/incremental`（`changed:["util.h"]`） | 200；`affectedFiles: [main.c, net/open.c, net/runner.h, util.h]`，同名头 `include/util.h` 未误报 |
| `curl POST /v1/scan/incremental`（`deleted:["svc/b.h"]`，文件已真实删除） | 200；`svc/b.h` 节点与入边移除；`svc/a.h` 及其传递依赖方均列入受影响；`cycles: []` |
| 宏 include 请求 | 422：`x.c: line 2: macro-generated #include is not supported: "DYNAMIC_MACRO"` |
| 缓存目录=源码根 | 400：`cache directory ... must be separate from source root ...` |
| 无缓存直接增量 | 409：`no cached scan for this source root; run a full scan first` |

**未通过项**：无。开发过程中曾发现并修复一个真实缺陷——增量删除时若先删
节点再算反向闭包，被删文件的依赖方会漏报；现改为基于删除前的旧图计算
受影响集合（有对应回归测试 `TestIncrementalDeletedFile`）。

## 已知限制

- 只支持 `#include "..."` / `#include <...>` 两种形式；宏生成 include、
  `#include_next`、条件编译（`#if`/`#ifdef`）均不处理（宏 include 会显式报错）。
- 字符字面量中含 `"` 的极端情况（如 `'"'`）不在子集范围内。
- 增量接口不做文件系统监听，变更集合完全由调用方显式声明。
