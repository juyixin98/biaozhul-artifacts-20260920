# RUNLOG — 实际运行记录

环境：`go version go1.22.2 linux/amd64`，Linux 6.8.0-90-generic，x86_64。
下列命令均在仓库根目录实际执行；时间为开发当日（2026-09-24）本地时间。

## 1. 自动化测试

### 1.1 `go test ./... -count=1`

结果：**全部通过，0 失败**（36 个测试函数，含表驱动子用例与穷举交叉校验）。

```
?   depsolver/cmd/depsolver        [no test files]   # 注：CLI 子进程测试见下，-cover 时为 ok
?   depsolver/internal/model       [no test files]
?   depsolver/internal/registry    [no test files]   # 注：补齐直接测试后为 ok 100%
ok  depsolver/internal/semver      0.002s
ok  depsolver/internal/service     0.008s
ok  depsolver/internal/solver      0.008s
```

补充 registry 直接测试与 CLI 子进程测试后的最终一次完整运行：

```
ok  depsolver/cmd/depsolver        0.442s   (TestCLISubprocess：go build 后跑真实二进制)
ok  depsolver/internal/registry    0.002s
ok  depsolver/internal/semver      0.002s
ok  depsolver/internal/service     0.009s
ok  depsolver/internal/solver      0.008s
```

统计（`go test ./... -v`）：`--- PASS` 36，`--- FAIL` 0。

### 1.2 竞态检测 `go test ./... -race -count=1`

结果：**通过**，无 data race 报告。

```
ok  depsolver/internal/semver      1.016s
ok  depsolver/internal/service     1.034s
ok  depsolver/internal/solver      1.037s
```

### 1.3 覆盖率 `go test ./... -cover`

```
depsolver/internal/registry   100.0%
depsolver/internal/semver      83.5%
depsolver/internal/service     84.3%
depsolver/internal/solver      84.9%
depsolver/cmd/depsolver        （覆盖率统计为 0%，但有 TestCLISubprocess 子进程冒烟）
```

未覆盖部分主要是防御性错误分支（JSON 编码失败、写盘 IO 错误等）；无“被测主路径无测试”的情况。

### 1.4 `go vet ./...` 与 `gofmt -l .`

均**无输出（干净）**。

## 2. CLI 实际运行

### 2.1 菱形冲突 + 回溯（`examples/resolve-backtrack.json`）

`go run ./cmd/depsolver resolve --request examples/resolve-backtrack.json`

- `satisfiable=true`
- 选择：`root=1.0.0, left=1.1.0, right=1.0.0, common=1.3.5`
- `stats`: `decisions=6, backtracks=2, conflicts=2, rejected_versions=1`
- trace 中可见完整顺序：`add-requirement → select-package → try → activate → … → backtrack → backtrack → try → activate …`，
  即先试 `left@1.2.0`（要求 common v2，与 right 的 `~1.3.0` 冲突），回退到 `left@1.1.0` 后成功。

### 2.2 无解（`examples/resolve-unsat.json`）

- 退出码 0（无解是正常业务结果，结构化冲突走 stdout；用法/IO 错误才是非零退出）。
- `conflict.type=constraint-clash`，`conflict.package=common`。
- message：`no version of "common" satisfies all of: >=2.0.0 (by left); ~1.3.0 (by right)`。
- `conflict.chain` 从 root → right → common；`conflict.rejected` 逐版本给出原因
  （`2.0.0 does not satisfy ~1.3.0 (required by right)` 等）。

### 2.3 预发布（`examples/resolve-prerelease.json`）

默认门控下，因 app 显式约束 `>=1.1.0-beta.1 <2.0.0`（提及 1.1.0 基元），
选中 `lib=1.1.0-rc.1`（rc.1 优先级高于 beta.2）。

### 2.4 循环（夹具 `testdata/fixtures/06-cycle.json`）

`satisfiable=true`，选择 `a=1.1.0, b=1.0.0`，并在 `cycles` 中报告 `[a b]`。

### 2.5 本地构建规划 `plan`（缓存/工作目录分离）

`mkdir -p /tmp/depsolver-demo/workspace /tmp/depsolver-demo/cache`
后运行 `depsolver plan --request examples/plan-request.json`：

- 锁文件：`/tmp/depsolver-demo/workspace/depsolver.lock.json`（schema `depsolver.lock/v1`）
- 缓存记录：`/tmp/depsolver-demo/cache/resolve-app.json`（schema `depsolver.cache/v1`）
- 选择 `app=1.0.0, web=2.1.0, log=1.4.2`
- 同样请求写入两个不同目录后 `cmp` 锁文件：**字节完全一致（无时间戳）**。

## 3. HTTP 接口实际运行（本地回环）

> 注：开发机上 `18080/18091` 已被环境内其他进程（`vccsim`、`ws`）占用，
> 首次按固定端口启动得到 `bind: address already in use`。改用由内核分配的空闲端口
> （34229）后，`ss -tlnp` 确认监听者是本次启动的 `depsolver` 进程，再执行下列请求。
> 测试结束后已停止该进程（`pgrep -x depsolver` 为空）。

- `GET /healthz` → `200 {"status":"ok"}`
- `POST /v1/resolve`（回溯样例）→ `200`，选择与 2.1 相同，trace 事件种类完整。
- `POST /v1/build/plan`（两个分离目录）→ `200`，`written.lockfilePath` 位于 workspace、
  `written.cacheRecord` 位于 cache；两个文件均在磁盘确认。
- `POST /v1/resolve`（无解样例）→ HTTP **409**，body 中 `conflict.type=constraint-clash`。
- `POST /v1/resolve` 带未知字段 `{"roots":[],"bogus":1}` → **400**
  `invalid JSON body: json: unknown field "bogus"`（严格解码）。
- 未知路由 `/nope` → **404**（ServeMux 默认行为）。

## 4. 离线边界

- `go.mod` 无任何 `require`；标准库之外零依赖。
- 全仓库非测试代码中 `net/http` 仅出现在 `cmd/depsolver`（`ListenAndServe`）与
  `internal/service`（请求处理）；不存在任何出站客户端调用。
- 静态守卫测试 `TestNoOutboundNetworkCalls` 扫描非测试源码中的
  `http.Get/http.Post/http.Client/net.Dial/grpc.Dial/sql.Open` 等符号，**通过**。

## 5. 穷举参考交叉校验

`internal/solver/reference_test.go` 独立枚举“每包取某版本或缺席”的完整组合空间，
在菱形可解、菱形回溯、无解、预发布门控（开/关）、循环 5 类小图上验证：

1. 求解器 SAT/UNSAT 结论与穷举一致（无解图穷举可行解数必须为 0）；
2. 成功路径上的每一次贪心选择都满足“不存在更新候选仍能补全为可行解”。

测试中打印的可行完整指派数量：diamond-compatible=4、backtrack=2、cycle=2。
**全部通过，无未通过项。**

## 6. 开发过程中出现并已修复的问题（如实记录）

1. caret 语义：`^1.2` 初版测试期望写成“锁到 1.2.x”，与简化 npm 语义（`^1.2` 允许
   1.3.0）不符——修正的是测试期望，代码行为保持 npm 语义。
2. 解析器两处健壮性：比较符 + 部分版本（`>=1.x`）应合法并展开；预发布空片段
   （`1.2.3-alpha..1`）最初被误接受，已在 `parseID` 补空串检查。
3. 冲突冒泡：初版回溯把最深冲突覆盖成顶层的 “root exhausted”，丢失了 common/ghost
   的解释链；已改为最深原始冲突保持为主解释、上层失败挂到 `cause` 链。
4. HTTP 固定端口被开发机上无关进程占用；改用内核分配端口并以 `ss` 确认进程身份，
   README 示例默认使用 8080 但可通过 `--addr` 指定。
