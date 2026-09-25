# depsolver — 依赖版本约束求解服务（纯后端）

一个**完全离线**的依赖版本约束求解器与本地构建工程服务：给定一个内存中的包注册表
（哪些包有哪些版本、每个版本依赖什么）和根工程的直接依赖，求出一组**满足全部约束的、
确定性的**版本集合；无解时给出可沿依赖链解释的冲突；求解过程中的每次选择与回溯都记录
在决策轨迹里。

- 语言/运行时：Go 1.22，**零第三方依赖**（`go.mod` 无 require）。
- 网络：**不连接任何云平台、不下载包**。所有数据来自请求体或本地夹具。仓库内唯一的
  `net/http` 用途是本地 JSON 服务端（见 `internal/service`）；离线边界由
  `TestNoOutboundNetworkCalls` 静态守卫。
- 工作目录与缓存目录分离：锁文件写入 `workspaceDir`，解析缓存元数据写入 `cacheDir`。
- 本项目只交付后端：CLI + JSON HTTP 接口 + 自动化测试，**不含前端**。

---

## 1. 目录结构

```
cmd/depsolver/            CLI 入口：serve / resolve / plan
internal/semver/          简化语义版本与区间约束（解析、比较、预发布门控）
internal/model/           领域模型与 JSON 请求类型
internal/registry/        内存注册表：校验、去重、版本降序索引
internal/solver/          回溯求解器、决策轨迹、冲突解释、循环检测、穷举参考测试
internal/service/         JSON HTTP 接口 + 本地落盘（锁文件/缓存）
testdata/fixtures/        验收夹具（01..08，_expect 块是自动化断言）
examples/                 请求样例
```

## 2. 构建与运行

```bash
go build ./...               # 编译
go test ./...                # 全部自动化测试
go test ./... -race -count=1 # 带竞态检测、禁用缓存
go vet ./...

go run ./cmd/depsolver resolve --request examples/resolve-backtrack.json
go run ./cmd/depsolver serve --addr 127.0.0.1:8080
```

### CLI

| 命令 | 作用 |
|---|---|
| `serve --addr 127.0.0.1:8080` | 启动本地 JSON HTTP 服务 |
| `resolve --request req.json [--prereleases]` | 离线求解，输出结果 JSON（含 trace） |
| `plan --request plan.json` | 求解并把锁文件/缓存记录写入两个分离目录（不下载任何内容） |

## 3. JSON 接口

### `GET /healthz` → `200 {"status":"ok"}`

### `POST /v1/resolve`

请求体：

```json
{
  "roots": [ { "name": "root", "constraint": "1.0.0" } ],
  "includePrereleases": false,
  "registry": {
    "packages": [
      {
        "name": "root",
        "versions": [
          { "version": "1.0.0",
            "deps": [ {"name":"left","constraint":"^1.0.0"} ] }
        ]
      }
    ]
  }
}
```

响应（SAT：`200`；UNSAT：`409`；请求非法：`400`）：

- `selections`：最终版本钉选（包名字典序，确定性）。
- `cycles`：选中图中的循环（名字归一化、去重、排序）。
- `conflict`：无解时的结构化解释（类型、冲突包、逐版本落选原因、到根的依赖链、
  回溯冒泡的 `cause`）。
- `trace`：完整决策轨迹（`add-requirement` / `select-package` / `try` / `activate`
  / `backtrack` / `conflict`），每条带搜索深度。
- `stats`：`decisions`、`backtracks`、`conflicts`、`rejected_versions`。

### `POST /v1/build/plan`

在 resolve 请求上额外给出 `workspaceDir`、`cacheDir`（均须为**已存在的绝对路径**）、
可选 `lockfileName`。SAT 时：

- 锁文件 `depsolver.lock/v1` 写入 **workspaceDir**（无时间戳，字节确定）；
- 缓存记录 `depsolver.cache/v1`（仅版本钉选元数据，无包内容）写入 **cacheDir**；
- UNSAT 返回 `409` 且**不写任何文件**。

## 4. 语义版本与约束语法

版本为 SemVer 2.0 的严格子集：`MAJOR.MINOR.PATCH[-prerelease][+build]`，
拒绝 `v` 前缀、前导零、四段版本。预发布严格按 SemVer 规则比较（数字按数值、
字母按 ASCII、数字标识 < 字母标识、带预发布 < 不带）；构建元数据可解析但不参与比较。

约束（`range-set`，组间 `||` 为“或”，组内空格/逗号分隔为“与”）：

| 写法 | 含义 |
|---|---|
| `1.2.3` / `=1.2.3` / `==1.2.3` | 精确等于 |
| `!=1.2.3` | 不等于 |
| `> >= < <=` + 版本或部分版本 | 比较（部分版本按区间端点展开，如 `<2.1` ⇔ `<2.1.0`） |
| `1` / `1.x` / `1.*` | `>=1.0.0 <2.0.0` |
| `1.2` / `1.2.*` | `>=1.2.0 <1.3.0` |
| `*` / `x` | 任意 |
| `^1.2.3` | 兼容区间：`>=1.2.3 <2.0.0`；`^0.2.3` 锁次版本；`^0.0.3` 锁修订号 |
| `~1.2.3` | 约等区间：`>=1.2.3 <1.3.0` |
| `>=1.0.0 <2.0.0`、`1.x || 2.x` | 与 / 或 |

**预发布门控（简化 npm 规则，默认开启）**：除非约束显式提及同一个
`MAJOR.MINOR.PATCH` 基元上的预发布（如 `>=1.1.0-beta.1` 显式提及 `1.1.0`），
否则预发布版本不会被选中；设 `"includePrereleases": true`（或 CLI `--prereleases`）
可关闭门控。

## 5. 求解策略与确定性

每个包在最终解中只允许一个版本（flat resolution）。搜索过程：

1. 根需求按包名排序并入；
2. 每个待决策包的候选版本按**语义版本降序**，先过滤出满足当前全部约束且通过门控的候选，
   并记录每个落选版本的原因；
3. 从最新候选开始 `try → activate（并入该版本的传递依赖）→ 递归`；
4. 子树失败则快照回滚并尝试更旧候选（`backtrack`，次数计入 `stats`）；
5. 全部候选失败则返回冲突；冲突在回溯冒泡时**保留最深的原始冲突为主解释**，
   上层失败挂在 `cause` 链上。

确定性来源：根依赖、包名、版本候选（注册表编译时降序）、同版本依赖并入顺序全部排序固定；
同一输入两次求解的 `selections` 与 trace 长度一致（见 `TestDeterministic`、
`TestPlanDeterministic`）。

循环依赖在 flat 模型下**不构成不可解**，但会在 `cycles` 中报告（DFS 三色标记、
环旋转归一化去重）。

## 6. 验收夹具（小依赖图穷举参考）

`testdata/fixtures/*.json` 同时是自动化输入和文档样例；`_expect` 块是断言（测试时剥离，
不进入求解器）：

| 夹具 | 场景 | 期望 |
|---|---|---|
| `01-diamond-compatible` | 菱形，约束相容 | SAT，common=1.2.1 |
| `02-diamond-backtrack` | 菱形，最新 left 冲突 | SAT，回溯到 left=1.1.0 |
| `03-unsat-diamond` | 菱形，无共同版本 | UNSAT，冲突在 common，链到 root |
| `04-prerelease-gate` | 默认门控 | SAT，跳过 beta 选 1.0.0 |
| `05-prerelease-explicit` | 显式提及预发布 | SAT，选 1.1.0-beta.2 |
| `06-cycle` | A↔B 循环 | SAT + cycles=[a,b] |
| `07-unknown-package` | 传递依赖缺失 | UNSAT，unknown-package=ghost |
| `08-multilevel-backtrack` | 多层回退 | SAT，≥3 次回溯 |

**穷举参考**（`internal/solver/reference_test.go`）独立于求解器实现：枚举“每个包取某版本
或缺席”的完整组合，判定可行完整指派，并验证两件事：

1. 求解器的 SAT/UNSAT 结论与穷举一致；
2. 贪心最优性：成功路径上每一步所选版本，都不存在“选一个更新版本仍能补全为可行解”的情况。

## 7. 请求样例

```bash
# 可解（含回溯）
go run ./cmd/depsolver resolve --request examples/resolve-backtrack.json
# 无解（查看 conflict.chain / rejected）
go run ./cmd/depsolver resolve --request examples/resolve-unsat.json
# 预发布
go run ./cmd/depsolver resolve --request examples/resolve-prerelease.json
# 落盘（plan 请求样例里已带绝对路径，先建目录即可）
mkdir -p /tmp/depsolver-demo/workspace /tmp/depsolver-demo/cache
go run ./cmd/depsolver plan --request examples/plan-request.json

# HTTP
curl -s -X POST localhost:8080/v1/resolve \
  -H 'Content-Type: application/json' --data @examples/resolve-backtrack.json
```

## 8. 设计边界与未做的事

- 不下载包内容、不校验包哈希、不执行构建脚本；“构建工程服务”只负责求解与确定性规划落盘。
- flat resolution：不支持同一包多版本并存（不做 npm 式嵌套安装）。
- 注册表不做版本范围声明（如 dist-tags）；`latest` 之类标签不支持，属于刻意收窄。
- 缓存记录只保存解析元数据；没有 TTL/失效策略（输入即内容，相同输入相同输出）。
