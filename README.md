# depresolve — 依赖版本约束求解器（纯后端）

一个离线的依赖版本约束求解服务：给定**本地提供的包注册表**（每个包有若干版本，每个版本声明自己的依赖与区间约束）和根需求，求解器用**回溯搜索**选出一组同时满足所有依赖的确定性版本；若无解，输出可解释的冲突与依赖链。

- 纯 Go 标准库实现，无第三方依赖，**不联网、不下载任何包**
- 本地 HTTP JSON 服务 + 命令行两个入口，无前端
- 缓存目录与工作目录分离（默认 `~/.cache/depresolve`），仅缓存本地纯计算结果
- 求解结果确定性：相同输入永远得到相同版本集与相同决策日志
- 小依赖图上用**穷举参考实现**交叉验证回溯求解器（含 400 个随机小图）

## 语义版本与约束

版本（`internal/semver/version.go`）：`MAJOR.MINOR.PATCH[-prerelease][+build]`，允许前导 `v`；
优先级遵循 SemVer 2.0.0 §11（数字标识按数值比、数字 < 字母、短预发布集 < 长集、正式版 > 同号预发布版），build 元数据不参与比较。

约束（`internal/semver/constraint.go`，简化 npm 风格）：

| 写法 | 含义 |
|---|---|
| `1.2.3` | 精确 |
| `>=1.2.0 <2.0.0` | 空格分隔为 AND |
| `^1.2.3` | 兼容区间：`>=1.2.3 <2.0.0`；`^0.2.3` 锁 minor，`^0.0.3` 锁 patch |
| `~1.2.3` | `>=1.2.3 <1.3.0` |
| `1.2.x` / `1.x` / `*` | 通配区间 |
| `>`, `>=`, `<`, `<=`, `=`, `!=` | 基本比较符 |
| `>=1.0.0 \|\| >=2.0.0` | `||` 分隔为 OR |

**预发布规则（npm 风格，简化）**：不含预发布比较符的约束集只匹配正式版；
若约束集中有某个比较符带预发布（如 `>=1.0.0-rc.1`），则只有与该比较符
`[major,minor,patch]` 三元组相同的预发布版才可能匹配（`1.0.0-rc.9` 可，`1.5.0-beta` 不可）。

## 求解算法（`internal/solver`）

- 每个包的可用版本预排序为**版本降序**。
- 深度优先回溯：每一步选择字典序最小的、已被要求但尚未选版本的包，从高到低尝试候选版本；
  选定后把该版本的依赖约束累积到对应包，再校验所有已选包仍满足累积约束。
  固定包顺序 + 候选降序，使**找到的第一个解就是确定性的偏好最优解**。
- 每个包只选一个版本（单版本安装模型，不支持并存多版本），循环依赖靠"已选包只校验不再展开"处理。
- 无解时记录最深冲突现场，给出冲突包、全部累积约束，以及从 root 沿已选包追溯的依赖链。
- `log` 完整记录回溯决策：`select / try / reject / conflict / backtrack / cycle / solution`，
  每条含序号、搜索深度、包、版本、原因。

`internal/solver/bruteforce.go` 是**穷举参考实现**（仅供测试）：枚举可达包所有版本组合（含"不选"），
筛出合法组合，并用与求解器相同的遍历偏好序选优。求解器测试对每个夹具和 400 个随机小图
断言两者在"可解性"与"确切版本集"上完全一致。

## 目录结构

```
cmd/solverd/        HTTP JSON 服务入口
cmd/depresolve/     单请求文件 CLI 入口
internal/semver/    版本解析、比较、约束解析与匹配
internal/solver/    回溯求解器 + 决策日志/冲突解释 + 穷举参考实现
internal/cache/     工作目录之外的磁盘缓存（SHA-256 键）
internal/api/       HTTP 路由、请求校验、缓存读写
examples/           请求样例夹具
```

## 构建与运行

需要 Go 1.22+（开发环境为 go1.22.2），无需任何外部依赖。

```bash
go build ./...
go test ./...
```

启动服务（默认 127.0.0.1:8080；缓存默认在用户缓存目录）：

```bash
go run ./cmd/solverd -addr 127.0.0.1:8080
# -no-cache            关闭磁盘缓存
# -cache-dir DIR       指定缓存目录（仍须在工作目录之外使用）
```

命令行求解单个请求文件：

```bash
go run ./cmd/depresolve -req examples/diamond-ok.json
```

## HTTP JSON 接口

### `GET /healthz`

返回 `{"status":"ok"}`。

### `POST /v1/solve`

请求体：

```json
{
  "registry": {
    "包名": [
      {"version": "1.0.0", "deps": {"依赖包": "约束", "...": "..."}}
    ]
  },
  "root": {"包名": "约束"},
  "options": {"disable_cache": false}
}
```

- 注册表必须随请求内联提供；服务不会按包名去任何远端查找。
- 引用了注册表中不存在的包、版本号/约束非法、版本重复 → `400 {"error": "..."}`。

成功响应（有解）：`ok: true`、`solution`（包→版本）、`log`（决策轨迹）。
无解也是 HTTP 200：`ok: false`、`conflict.package/reason/chains`。
缓存命中时附 `"cached": true`。

curl 示例：

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  --data @examples/diamond-ok.json \
  http://127.0.0.1:8080/v1/solve
```

菱形无解的冲突输出（`examples/diamond-conflict.json`）：

```json
{
  "ok": false,
  "conflict": {
    "package": "d",
    "reason": "no version of \"d\" satisfies all constraints: b@1.5.0 requires ^2.0.0; c@1.2.0 requires ^1.0.0",
    "chains": [
      "root => a ^1.0.0 -> a@1.0.0 => b ^1.5 -> b@1.5.0 => d ^2.0.0",
      "root => a ^1.0.0 -> a@1.0.0 => c ^1.2 -> c@1.2.0 => d ^1.0.0"
    ]
  }
}
```

## 请求样例（`examples/`）

| 文件 | 场景 |
|---|---|
| `diamond-ok.json` | 菱形依赖，b 1.5 与 c 对 d 的要求冲突，求解器回溯到 b 1.4 |
| `diamond-conflict.json` | 菱形冲突无解，输出两条 root→d 依赖链 |
| `prerelease.json` | 显式预发布约束选到 `1.0.0-rc.1` |
| `cycle.json` | a↔b 循环依赖，约束相容时有解 |
| `unsolvable.json` | 根约束无任何版本满足 |

## 自动化测试

```
go test ./...                  # 全部测试
go test -cover ./internal/... # 覆盖率
go test -v ./internal/solver  # 含 7 个固定夹具 + 400 个随机小图交叉验证
```

测试内容：
- `internal/semver`：合法/非法版本解析、SemVer §11 全序链、build 不参与比较、
  各类约束匹配、预发布门控。
- `internal/solver`：菱形回溯、菱形无解与依赖链、根约束直冲突、预发布选择、
  循环依赖（有解/无解）、未知包报错、结果确定性（含日志字节级一致）、偏好最高版本；
  以及与穷举参考的 7 夹具 + 400 随机小图交叉验证。
- `internal/api`：正常求解、无解、各类 400、healthz、缓存命中/绕过、缓存目录在工作目录之外。
- `internal/cache`：存取、键的顺序无关性、空目录报错。

## 实际运行记录（2026-09-24，Linux x86_64，go1.22.2）

以下命令与输出为实际执行结果（未通过项：无）。

构建与静态检查：

```
$ go build ./... && go vet ./...
# 无输出，退出码 0
```

测试（`go test -count=1 ./...`）：

```
ok  	depresolve/internal/api      0.006s
ok  	depresolve/internal/cache    0.004s
ok  	depresolve/internal/semver   0.003s
ok  	depresolve/internal/solver   0.041s
（两个 cmd 包无测试文件）
```

覆盖率（`go test -cover ./internal/...`）：api 80.3%、cache 69.6%、semver 92.0%、solver 92.9%；
`go test -v` 全量通过：29 个顶层用例 + 407 个子用例（7 个固定夹具 + 400 个随机小图），
共 436 项 PASS、0 项 FAIL。

CLI 实跑：

```
$ go run ./cmd/depresolve -req examples/diamond-ok.json
# solution: a=1.0.0 b=1.4.0 c=1.2.0 d=1.2.0；log 含
# try b 1.5.0 -> conflict d (^2 vs ^1) -> backtrack -> try b 1.4.0 -> solution

$ for f in examples/*.json; do go run ./cmd/depresolve -req $f -no-cache; done
# cycle.json        -> ok, a=1.0.0 b=1.0.0（log 含 cycle 条目）
# diamond-conflict  -> ok=false, conflict.package=d, 2 条依赖链
# diamond-ok        -> ok, 回溯后 b=1.4.0 d=1.2.0
# prerelease.json   -> ok, lib=1.0.0-rc.1
# unsolvable.json   -> ok=false, conflict.package=x, root => x >=2.0.0
```

HTTP 服务实跑（注：本机 8080/18080/18099 等端口被其他进程占用，
改用系统分配的空闲端口 37985，功能与默认端口一致）：

```
$ solverd -addr 127.0.0.1:37985
$ curl -s http://127.0.0.1:37985/healthz
{"status":"ok"}
$ curl -s -X POST --data @examples/diamond-ok.json .../v1/solve
# 200，solution 同上；第二次相同请求返回 "cached": true
$ curl -s -X POST --data @examples/diamond-conflict.json .../v1/solve
# 200，ok=false，conflict.chains 含 b@1.5.0 / c@1.2.0 两条链
$ curl -s -X POST --data '{"registry":{}}' .../v1/solve
# 400 {"error":"registry must be non-empty"}
```

缓存落盘位置验证：缓存文件写入 `~/.cache/depresolve/<sha256>.json`，
项目工作目录下无任何缓存文件；也有测试用例强制断言缓存目录不在工作目录内。

## 范围与限制（有意为之的简化）

- 注册表随请求内联，无包下载、无锁文件、无版本持久化仓库。
- 单版本安装模型：同一包在结果中只出现一个版本。
- 约束为简化 npm 语义；不支持 hyphen 区间、`>` 在预发布上的 npm 复杂规则、构建元数据匹配。
- 穷举实现按设计是指数级，只在测试的小夹具上使用。
