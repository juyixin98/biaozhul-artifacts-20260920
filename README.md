# depscanner — 增量 C include 依赖扫描服务（纯后端）

从给定的编译目标（`.c`/`.h` 等源文件）出发，扫描 C 风格 `#include` 子集，
构建**稳定排序的依赖图**，并基于持久化基线做**增量分析**：输出新增/修改/删除的
文件以及受其影响、需要重新构建的目标。

- 纯 Go 实现（Go 1.22），无第三方依赖；
- JSON HTTP 接口 + 同名 CLI 子命令，**不连接任何云平台**；
- 缓存（基线）目录与项目工作目录**强制分离**；
- 只解析本地文件，不执行编译、不执行夹具中的任何程序。

## 1. 支持的 include 子集

| 写法 | 搜索顺序 |
|---|---|
| `#include "a/b.h"`（引号） | ① 包含者所在目录 → ② `quote_include_dirs` → ③ `system_include_dirs` |
| `#include <a/b.h>`（尖括号） | 仅 `system_include_dirs` |

词法层面正确处理：

- 行注释 `//`、块注释 `/* ... */`（可跨行）中的伪 `#include`；
- 字符串 `"..."` / 字符常量 `'...'` 中的 `#include` 文本；
- 反斜杠换行的行拼接（`#inc\<换行>lude "x.h"` 会被正确识别）；
- CRLF、UTF-8 BOM、`#  include` 多余空白；
- 同名头文件：不同目录下的同名文件解析为不同绝对路径，是图中不同节点；
- 嵌套路径（`util/detail/format.h`）与菱形包含（边去重）。

**明确不支持并报错：宏生成 / 计算型 include**

```c
#define HDR "x.h"
#include HDR      // error: macro-generated/computed #include is not supported
#include (HDR)    // 同上
```

该限制符合需求：扫描器不做宏展开，凡是 include 后面不是 `"..."` 或 `<...>` 的，
一律产生 `severity=error` 诊断，响应 `status` 为 `"error"`，CLI 退出码为 1。

### 循环引用

DFS 遍历中通过“在途栈”识别回边，循环被记录为规范化（旋转到最小成员开头）后
去重的环，例如 `["a.h", "b.h", "a.h"]`，遍历不会无限递归。

## 2. 目录结构

```
cmd/depscanner/          CLI（serve / scan / affected / parse）
internal/scanner/        词法扫描：include 提取、注释/字符串/续行、宏 include 报错
internal/resolver/       引号 vs 尖括号搜索路径解析
internal/builder/        DFS 依赖图、循环检测、缺失节点、增量缓存接口
internal/service/        业务编排、基线快照持久化、JSON HTTP 接口
testdata/demo/           验收夹具（同名头/嵌套/注释伪include/循环/宏/缺失/删除）
examples/                请求样例与可运行演示脚本
```

## 3. HTTP JSON 接口

`GET /healthz`

`POST /api/v1/scan` — 全量（带缓存复用）扫描，并把结果写为基线：

```json
{
  "root": "/path/to/project",
  "targets": ["app/main.c"],
  "quote_include_dirs": [],
  "system_include_dirs": ["sysinc"],
  "cache_dir": "/tmp/depscanner-cache"
}
```

`POST /api/v1/affected` — 对比基线做增量分析，字段同上，另可带
`"update_baseline": true`（分析后刷新基线）。响应关键字段：

```json
{
  "status": "ok",
  "config_changed": false,
  "changed":  { "added": [], "modified": ["src/h/base.h"], "deleted": [] },
  "affected_files":   ["src/h/base.h", "src/main.c"],
  "affected_targets": ["src/main.c"],
  "graph": { "nodes": [...], "edges": [...], "cycles": [...], "stats": {...} }
}
```

`POST /api/v1/parse` — 只解析单文件的 include（不解析搜索路径）：

```json
{ "root": "/path/to/project", "file": "app/common.h" }
```

约束与错误语义：

- 所有路径中 `root`、`targets`、`file` 必须位于项目根内（禁止 `..` 逃逸）；
- `cache_dir` 不得等于或嵌套于 `root`（反向亦然），返回 400；
- 请求 JSON 非法或含未知字段返回 400；文件存在性/宏 include 等语义问题
  不影响 HTTP 状态码（200），而是体现在响应 `status` 与 `diagnostics` 中；
- 无法解析的 include 产生 `missing` 节点与 `warning` 诊断（不致命）；
- 基线不存在或扫描配置变化时，`config_changed=true`，按“全部目标受影响”处理。

完整请求样例见 `examples/*.json`。

## 4. CLI 用法

```bash
go build ./cmd/depscanner

# 本地服务
./depscanner serve --addr 127.0.0.1:8080

# 扫描并写基线（--quote-dirs/--system-dirs/--targets 均可重复）
./depscanner scan --root testdata/demo --cache-dir /tmp/cache \
  --targets app/main.c --system-dirs sysinc

# 增量分析（不改基线）
./depscanner affected --root testdata/demo --cache-dir /tmp/cache \
  --targets app/main.c --system-dirs sysinc

# 增量分析并更新基线
./depscanner affected ... --update-baseline

# 单文件词法解析
./depscanner parse --root testdata/demo --file app/common.h
```

## 5. 增量与缓存设计

- 基线文件：`<cache_dir>/depscanner-root-<root哈希前缀>.json`，原子写
  （临时文件 + rename）；包含文件哈希、size、mtime、include 列表、图结构与配置。
- 单文件复用分两级：
  1. **快路径**：`size + mtime` 一致（且 mtime 来自本进程 stat，单调时钟可信）→
     完全复用，不读文件内容；
  2. **hash 校验**：跨进程从快照恢复时单调时钟已丢失，且部分文件系统 mtime
     粒度很粗（本项目在开发机上实测到同一秒内快速重写返回**相同 mtime+size**），
     因此再读内容算 SHA-256 兜底，哈希一致才复用，避免漏报修改。
- 变更分三类：`added` / `modified`（哈希或 include 列表变化）/ `deleted`。
  删除的文件已不在当前图中，受影响集合在**旧图**上做反向可达追溯，与当前图
  结果取并集，因此删除仍能正确定位受影响目标。
- 缓存目录与工作目录物理分离，项目目录内不会产生任何缓存文件。

## 6. 输出稳定性

节点按路径、边按 `(from, line, to)`、环按字典序排序；节点使用相对 root 的
正斜杠路径（项目外文件用绝对路径）作为稳定键；重复运行输出除 mtime 外完全一致
（测试 `TestBuild_StableOutputAcrossRuns` 覆盖）。

## 7. 自动化测试

```bash
go test ./...            # 单元 + 集成（含 httptest 的端到端接口测试）
go test -race ./...      # 竞态检测
go vet ./...
```

测试覆盖验收点：同名头区分、嵌套路径、注释/字符串伪 include、反斜杠续行、
宏 include 报错、循环与自环、菱形去重、缺失文件、文件删除、修改、新增、
配置变更、缓存目录分离、缓存复用与粗粒度 mtime 兜底、HTTP 400/200 语义。

手工端到端演示（会在临时目录中修改/删除夹具副本，不污染仓库）：

```bash
bash examples/demo.sh       # CLI
bash examples/curl-demo.sh  # HTTP
```

## 8. 已知边界

- 不做任何宏展开（除识别“宏 include 并报错”外），不处理条件编译
  （`#if` 的分支选择）：所有可达分支中的 include 都会被计入；
- include 路径中不识别注释（这符合 C 标准）；
- 不扫描目标可达集合之外的文件；系统目录中不存在的头按缺失节点处理。
