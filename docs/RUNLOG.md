# 运行记录（RUNLOG）

日期：2026-09-24。环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，
bash 5.x。本文如实记录实际执行的命令、结果以及过程中出现并修复的失败项。

## 1. 最终测试结果

命令：

```bash
go vet ./...
go test -count=1 ./...
go test -race -count=1 ./...
go build -o bin/buildprov ./cmd/buildprov
```

结果：`go vet` 无输出（通过）；测试全部通过，共 25 个测试函数
（service 14、provenance 4、executor 3、canonical 3、httpapi 1 个端到端）：

```
?       buildprovenance/cmd/buildprov   [no test files]
ok      buildprovenance/internal/canonical      0.002s
ok      buildprovenance/internal/executor       0.035s
ok      buildprovenance/internal/httpapi        0.063s
ok      buildprovenance/internal/provenance     0.011s
ok      buildprovenance/internal/service        0.981s
```

`-race` 运行同样全部通过（约 6.7s 总墙钟）。无未通过项遗留。

## 2. 端到端演示结果（真实运行）

启动：

```bash
./bin/buildprov -addr 127.0.0.1:18791 \
  -data-dir /tmp/bpdemo/data -work-dir /tmp/bpdemo/work -fixtures ./fixtures
./scripts/demo.sh http://127.0.0.1:18791
```

退出码 `0`。关键实测结果（完整输出见下，也保存于开发期临时目录
`/tmp/bpdemo/demo-output.txt`）：

1. 注册 4 个源码、3 个夹具动作均返回 `HTTP 201`。
2. 构建出共享依赖图，制品 ID 为内容派生：
   - `libA = art_1925919afcbffd95`（common.js + appA.js）
   - `libB = art_1f59799b0a464ccf`（common.js + appB.js）
   - `binA = art_39f7f9dd692f6199`（appA.js + libA）
   - `binB = art_c80c903ee42a9e9c`（appB.js + libB）
3. `POST /v1/artifacts/<binA>/verify` → `HTTP 200`，
   `complete=true depth=1 records=2 blobsRehashed=6 issues=null`。
4. `GET /v1/impact?source=src/common.js` → 4 个制品全部受影响（两个 lib
   与两个 bin），证明共享依赖的传递闭包正确。
5. 时间嵌入夹具 `nondet.sh`：`verify.complete=True`，但
   `reproduce.reproducible=False`（BIN 槽 `match=False`）——完整溯源不等
   于可复现。
6. 直接覆写 libA 在 CAS 中的内容字节后重新核验：
   - libA：`HTTP 422`，报 `ARTIFACT_DIGEST_MISMATCH`、
     `OUTPUT_SIZE_MISMATCH`、`RECORD_OUTPUT_DIGEST_MISMATCH`；
   - 下游 binA：`HTTP 422`，报 `INPUT_DIGEST_MISMATCH`（及其余链上项）。
7. 路径穿越工具 `["bash","../../../etc/passwd"]` → `HTTP 400`。

另一干净实例（端口 18792）上的确定性复核：同样输入产生**完全相同**的制品
ID（`art_1925919afcbffd95`、`art_39f7f9dd692f6199`），`reproduce` 返回
`recordedDigest == reproducedDigest ==
sha256:131586c985a37da774915b9c27c6fb58e3ec3d16578988180810afdd074bca7a`。

8. 持久化与重启恢复（修复 3.8 之后复测）：注册源码/工具→构建→杀掉进程→
   用同一 data-dir 新端口重启，`index/` 下有 `sources.json artifacts.json
   tools.json`，重启后 `GET /v1/tools/compile_lib` 恢复为
   `sha256:38f5a7a2…`，对重启前制品 `POST /verify` 返回
   `complete=true records=1 blobsHashed=3 issues=null`。
9. 修复全部缺陷后的最终一轮 `scripts/demo.sh` 运行 `demo exit=0`，第 7 步
   篡改检测输出与上文第 6 点一致（libA 422 三项 issue、binA 422 含
   `INPUT_DIGEST_MISMATCH`）。

## 3. 开发过程中出现的失败及修复（如实记录）

### 3.1 编译失败：未使用变量

- 现象：`go build` 报
  `internal/service/reproduce.go:54:2: target declared and not used`。
- 原因：重构复现逻辑时遗留了未再使用的 `target` 局部变量。
- 修复：删除该变量。重新 `go build ./...` 通过。

### 3.2 类型不匹配

- 现象：`go vet` 报
  `cannot use "sha256:" + strings.Repeat(...) (string) as provenance.Digest`。
- 修复：显式转换为 `provenance.Digest(...)`。

### 3.3 全部构建类测试首次运行失败（真实缺陷）

- 现象：11 个测试首次执行时全部报
  `open .../work/build-XXXX/out/LIB: no such file or directory`。
- 根因：`executor.Run` 用 defer 在**函数返回时**即删除私有工作目录，而
  调用方 `ExecuteAction`/`Reproduce` 是在 `Run` 返回之后才读取输出文件——
  文件先被读到结果列表、随即被删除，读取即失败。手动以相同环境变量布局
  直接跑夹具可成功（exit 0、输出存在），从而确认问题在服务的清理时机而非
  夹具本身。
- 修复：`Run` 成功时保留工作目录并在 `Result` 中交回路径，由调用方在读完
  输出后删除；普通命令失败时由执行器自行清理，声明输出缺失时保留目录供
  诊断。修复后构建类测试全部通过。

### 3.4 伪造循环导致栈溢出（真实缺陷）

- 现象：引入循环伪造测试后，`go test` 崩溃：
  `runtime: goroutine stack exceeds 1000000000-byte limit ... stack overflow`，
  递归发生在 `service.depth`。
- 根因：`depth` 的记忆化只缓存“已完成”节点，没有“访问中”标记；在伪造的
  A↔B 环上无限互递归。
- 修复：增加 `visiting` 集合，遇到访问中节点返回 0（环由独立的
  `findCycle`/`CYCLE_DETECTED` 负责报告，深度计算保持有限）。修复后通过。

### 3.5 演示脚本变量展开错误

- 现象：`scripts/demo.sh` 首次运行到构建阶段报
  `line 53: rest: unbound variable`（脚本开了 `set -u`）。
- 根因：在同一条 `local slot=... rest=...` 复合声明里做参数展开，bash
  在此写法下对右侧展开的处理触发未绑定判定。
- 修复：拆分为普通赋值语句。重跑 `demo-exit=0`。

### 3.6 演示启动方式自伤

- 现象：一次重启脚本中使用 `pkill -f 'buildprov -addr 127.0.0.1:18791'`，
  命令行模式也匹配到执行该命令的自身 shell，导致整批命令以 144 退出、服务
  未保留。
- 修复：改为启动时写 PID 文件、用 `kill $(cat pid)` 停止。属演示脚本操作
  问题，非产品代码缺陷。

### 3.7 端口占用

- 现象：首次启动监听 18080 报
  `bind: address already in use`（主机上该端口已被他用），健康检查打到了
  无关服务（`404 page not found`）。
- 处理：换用高位空闲端口 18791/18792。属环境问题。后续脚本改用内核分配的
  空闲端口（先 bind :0 取端口）以稳定规避。

### 3.8 工具注册未持久化，重启后历史制品无法核验（真实缺口）

- 现象：用同一 data-dir 重启服务后，`verify` 对重启前构建的制品返回
  `TOOL_NOT_FOUND`（compile_lib/link_app 仅存于内存）。源码与制品索引会落盘，
  工具定义却不会。
- 修复：新增 `index/tools.json` 持久化与启动加载；服务在注册工具、注册源码、
  执行动作后通过 `onChange` 回调即时落盘（另有 10 秒定时与退出兜底）。
  注意工具的可信摘要仍在 `verify` 时从磁盘上的解释器/夹具文件**重新推导**，
  持久化文件只负责恢复“注册了哪些动作”。
- 复测：同一 data-dir 换端口重启后，工具恢复为 `compile_lib
  sha256:38f5a7a2…`，`art_1925919afcbffd95` 核验
  `complete=true records=1 blobsHashed=3`。

### 3.9 onChange 回调在持锁状态下自死锁（真实缺陷）

- 现象：3.8 的回调接入后，`POST /v1/tools` 请求挂起，curl 超时，服务不响应。
- 根因：`RegisterTool` 持有 `s.mu` 时调用 `notify()`，而主机回调里的
  `SaveTools` 又要获取同一把 `sync.Mutex`——Go 互斥锁不可重入，自死锁。
- 修复：将 `RegisterTool` 改为手动加锁/解锁，先把工具放入 map 并解锁，再调用
  `notify()`；新增回归测试 `TestOnChangeDoesNotDeadlock`，在回调中真正调用
  `SaveTools`（修复前该测试永久挂住）。`-race` 全量通过。

## 4. 未通过项 / 已知边界

- 最终状态下**无未通过测试**。
- 已知边界（设计如此，非失败项）：
  - HMAC 密钥是服务本地随机密钥，提供篡改检测，不提供跨组织信任；跨组织
    需替换为非对称签名（记录哈希与签名字段已分离）。
  - 仅监听回环、无身份鉴权；定位为本地开发工具。
  - 证明日志被篡改后服务的策略是**拒绝启动**（fail-closed），不提供在线
    “跳过坏行继续服务”路径。
  - 非确定性动作的重复构建：若同一动作+输入产生了与已记录不同的输出摘要，
    服务拒绝覆盖既有溯源并返回 non-deterministic 冲突错误。
