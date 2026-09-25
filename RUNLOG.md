# 运行记录（2026-09-24，Go 1.22.2，linux/amd64）

以下均为本机实际执行过的命令与结果。

## 1. 单元测试 + 竞态检测

```bash
$ go test -race ./...
?   traceassembly/cmd/demo          [no test files]
?   traceassembly/cmd/trace-server  [no test files]
ok  traceassembly/internal/httpapi  1.020s
ok  traceassembly/store             1.033s
ok  traceassembly/trace             1.017s
```

13 个测试用例全部通过（`go test -v` 中 13 个 `--- PASS`，0 个 `--- FAIL`）。
覆盖率：trace 80.3%、store 80.5%、httpapi 75.9%（`go test -cover`）。

覆盖的场景：乱序（孙→子→根）、缺根超时+迟到修订+逐版包含、精确重复、
负载冲突（先到先得+冲突字段+跨版累积）、时钟偏差（含容忍带反例）、双节点环、
自环、多根、纯水位超时（无 sleep/壁钟）、整批校验回滚、修订不可变、
JSON 形态、WAL 两次重启 `reflect.DeepEqual` 一致性、HTTP 全链路（含 404/400）、
精确重复不重开已关闭 trace。

## 2. 合成场景自检程序

```bash
$ go run ./cmd/demo
... 三幕 JSON 报告（A 缺根迟到 / B 重复冲突 / C 偏差循环）...
demo: ALL CHECKS PASSED   # 退出码 0
```

## 3. HTTP 端到端走查（真实启动进程 + curl + jq 断言 + 重启重放）

```bash
$ ./examples/walkthrough.sh
...
E2E: ALL ASSERTIONS PASSED   # 退出码 0
```

25 条断言全部 PASS，涵盖：超时不完整标志与原因、悬空父边、迟到 rev2
（revision=2、revised_of=1、complete=true）、rev1⊆rev2 包含、历史版本冻结、
重复/冲突分类字段、偏差两类告警且不影响结构、循环归一化输出、重启后水位
（=500）、四个 trace 与 rev2 经 WAL 重放逐版可查。

## 4. 静态检查与格式

```bash
$ go vet ./...    # 无输出（通过）
$ gofmt -l .      # 无输出（全部合规）
$ grep -rn time.Now trace/   # 仅测试注释中出现；核心逻辑无 time.Now 调用
```

## 开发过程中发现并修复的问题（如实记录）

1. **包名不一致**：核心目录 `trace/` 初版声明为 `package traceassembly`，
   编译报 “imported as traceassembly and not used”，已统一为 `package trace`。
2. **手写时间辅助类型错误**：demo 里自造的 duration/time 包装类型无法通过编译，
   改为标准库 `time.Time`/`time.Duration`。
3. **端口冲突**：首次走查固定用 18080，被机器上已有的 `vccsim` 进程占用，
   请求打到了对方服务返回 404；脚本改为每次动态选取空闲端口，并在健康检查中
   增加“服务进程提前退出则报错”的检查。
4. **真实语义缺口（最重要）**：初版仅在水位推进时扫描超时。当全局水位已到 500
   后，新 trace 携带旧的 receive_ns（如 20）随 `at_least_watermark_ns=200`
   （小于当前水位）到达时，批次结束不会触发关闭，且该批边界无法在 WAL 重放中
   复现。修复方案：每批摄入结束按当前水位补一次扫描，并把扫描点作为
   `{"type":"sweep"}` 事件写入 WAL，重放时在同一位置重放扫描（`RestoreSweep`）。
   修复后 e2e 场景 4、5（偏差、循环）与重放一致性全部通过；并补充了
   “精确重复不重开已关闭 trace”的回归测试。

## 未通过项 / 已知未做

- 无未通过的测试或断言（最终状态下上述 1..3 全部退出码 0）。
- 未实现（README“局限”已声明）：多副本/分片、TTL 与修订压缩、时钟偏移估计；
  WAL 每条 fsync 仅为样例级吞吐。
