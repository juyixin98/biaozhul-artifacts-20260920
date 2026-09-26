# RUNLOG — 实际运行记录

本文件如实记录开发与验收过程中执行的命令、结果、中途失败项及其根因/修复，不做美化。
所有命令均在项目根目录执行。

## 1. 环境

| 项 | 值（实际输出） |
|---|---|
| 日期/时间 | `Fri Sep 25 15:26:25 CST 2026` |
| 内核 | `Linux ... 6.8.0-90-generic ... x86_64 GNU/Linux` |
| JRE | `openjdk version "21.0.12.1" 2026-08-18` (Ubuntu 24.04 打包) |
| javac | `javac 21.0.12.1` |
| 默认时区 | `Asia/Shanghai` |
| **tzdata 版本** | **`2026b`**（来自 JDK 内置 `java.time`，由 `/health`、`/info`、`/simulate` 与状态文件记录） |
| 第三方依赖 | 无（仅 JDK） |
| 代码量 | 主代码 14 个 `.java`（约 1914 行），测试 10 个 `.java`（约 751 行） |

## 2. 构建与自动化测试（最终结果）

```bash
rm -rf build
scripts/run-tests.sh
```

结果：

```
Tests run: 35, passed: 35, failed: 0
```

进程退出码 `0`（测试通过且 JVM 正常退出）。

> 说明：测试框架不是 JUnit，而是内置的迷你反射 runner（`src/test/java/hlc/TestRunner.java`），
> 以保证在完全离线、无 Maven/Gradle 的环境也能编译运行。运行器类清单里会列出
> `hlc.Test`（它是注解类型，本身无测试方法，属正常现象，不计入 35）。

## 3. 中途真实失败与修复（未通过项历史）

开发过程中测试**并非一次通过**，以下为实际遇到的问题及处置：

1. **主代码编译错误（首轮 `javac`）**
   - `SeekableByteChannel` 没有 `force(boolean)` 方法 → 改用 `FileChannel.open(...).force(true)`。
   - `DemoScenarios` 缺少 `import java.nio.file.Path;`。
   - `HLCTimestamp.compareTo` 中误留一行非法草稿代码 → 删除。
   - 修复后主代码编译通过。

2. **测试编译：classpath 缺失**
   - 现象：`cannot find symbol ... Json/HLCException`（228 个错误）。
   - 根因：用 `javac -d` 编译测试时未把主类输出目录加入 classpath。
   - 修复：`scripts/build.sh` 中测试编译改为 `javac -cp build/classes ...`。

3. **首轮测试运行：8 个失败（35 中 27 通过）**，逐条定位：
   - **因果引擎方向判反（核心）**：happens-before 闭包存的是"前驱集合"，判定
     `a→b` 时却错误地查了 `closure[a]` 是否含 `b`，造成大量"伪违例"。
     修复为 `a→b  ⇔  closure[b].contains(a)`，同时修正并发判定。该错误只在测试/报告
     逻辑中，**不在** `HLCClock` 时间戳算法里。
   - **未知节点 NPE**：`ApiService.pinPhysical` 对不存在的节点直接解引用。
     修复为先判空并抛 `HLCException("unknown node ...")`。
   - **测试期望写错（send/receive 计数）**：新节点收到领先消息时按 HLC 合并规则计数
     应为消息计数 +1，最初期望值少算 1；修正测试期望。
   - **`List.of(..., null)` 抛 NPE**：JDK 不可变集合不允许 null，JSON 测试改用
     `ArrayList` 容纳 JSON `null`；`Json::parseObject` 的非法引用改为传入 `[1,2]`。

4. **HLC 计数约定不一致（demo 输出复核时发现的真问题）**
   - 现象：接收事件在物理时间严格领先时给出 `c=0`，而本地事件在同样情况下给 `c=1`，
     两种约定混用。
   - 决策：统一采用 HLC 论文（Kulkarni et al., 2014）约定——**物理时间严格领先并决定
     新 `l` 时 `c=0`；仅当 `l` 不变时计数自增**。重写 `tickLocal`/`receive`，并同步
     更新全部相关测试与 demo 期望。修复后时间戳序列在所有场景下自洽。

5. **测试 JVM 挂死不退出**
   - 现象：`timeout` 杀掉进程，退出码 `124`。
   - 根因：`com.sun.net.httpserver` 内部 dispatcher 为非守护线程，集成测试起了服务却
     没停止。
   - 修复：HTTP 工作线程池改为守护线程；测试框架新增 `@AfterAll` 钩子，集成测试
     结束时 `server.stop()`。复测退出码 `0`。

6. **HTTP 演示端口冲突（环境问题，非代码缺陷）**
   - 现象：`demo-http.sh` 请求得到与本项目完全不同的响应信封（`success/data/error`、
     `clockType:virtual`），且状态文件未生成。
   - 排查：服务器日志 `BindException: Address already in use`；`ss -ltnp` 显示本机
     `8088`（pid 282106）和 `8080`（pid 18689）已被**机器上其它预先存在的 Java 服务**
     占用。未去动这些无关进程。
   - 修复：演示脚本默认请求端口 `0`，由操作系统分配空闲端口，并从启动日志解析实际端口。
     此后端到端演示稳定通过。

7. **空 body 的 POST 被拒**
   - `/persist/save`、`/persist/load` 用空 body 调用时被判为非法。修复：空 body 视为
     空 JSON 对象，由各处理器自行校验所需字段。

## 4. 功能级手工验证（实际命令与结论）

### 4.1 内置固定数据 demo

```bash
java -cp build/classes hlc.cli.Main demo
```

关键结论（解析后的实际值）：

- tzdb：`2026b`。
- 三节点带偏差交错：`soundnessHolds=true`，`violations=0`；
  例：`a1=1000000:0`，`b1` 在物理领先时 `1000100:0`，`c2` 在 carol 物理回退到 900000
  时仍得到 `1000600:1`（l 取本地最大、c 自增）。
- 物理回退序列 `pt 10000→5000→1000→10000`：
  `10000:0 → 10000:1 → 10000:2 → 10000:3`，严格单调。
- 计数溢出：在 `5000:9223372036854775807` 再 tick → 抛出 `LogicalCounterOverflowException`，
  状态不变；物理前进到 5001 后 → `5001:0`，`counterReset=true`。
- 持久化恢复：`monotonicAcrossRestart=true`，`7000:1 →（重启，墙钟回退到 6000）→ 7000:2`。

### 4.2 CLI 跨进程持久化（load→操作→save）

```bash
rm -f /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main nodes create --node alpha --state /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main tick   --node alpha --pt 5000 --state /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main tick   --node alpha --pt 5000 --state /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main tick   --node alpha --pt 1000 --state /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main send   --node alpha --pt 5000 --state /tmp/cli-state.properties
java -cp build/classes hlc.cli.Main receive --node beta --pt 4000 --message 5000:3 --state /tmp/cli-state.properties
```

实际时间戳演进：`5000:0 → 5000:1 →（物理回退到1000）5000:2 → 5000:3 →（beta 物理仅4000）5000:4`。
状态文件实际包含 `node.alpha=5000\:3`、`node.beta=5000\:4`、`tzdb=2026b`、`version=1`。

### 4.3 CLI 仿真：证明"时间戳先后 ≠ 因果"

```bash
java -cp build/classes hlc.cli.Main simulate samples/07-simulate-concurrent.json
```

输出：`soundnessHolds=true`，`violations=0`，
`concurrentButTimestampOrdered` 含 4 对，其中见证对
`(y1=1000:0) < (x1=2000:0)` 被标记 `happensBefore=false`——两节点从不通信，时间戳
次序是任意的、非因果的。

### 4.4 HTTP 端到端

```bash
scripts/demo-http.sh          # 默认 HLC_PORT=0，使用 OS 分配的空闲端口
```

实际端口一次为 `42653`。关键实际响应：

- `GET /health` → `{"status":"ok","tzdb":"2026b"}`
- 创建 alice/bob；`/tick` → `1000000:0`；`/send` → `1000000:1`（含可传递 `message`）；
  bob 在物理 `950000` 落后时 `/receive` 消息 `1000000:1` → `1000000:2`。
- `POST /persist/save` → `{"saved":true,"nodes":2,"tzdb":"2026b",...}`；磁盘文件：
  ```
  node.alice=1000000\:1
  node.bob=1000000\:2
  tzdb=2026b
  version=1
  ```
- `/simulate` 两个样例分别得到 `violations=[]` 与若干 `happensBefore:false` 的时间戳有序对。
- 非法 JSON `POST /tick` → `HTTP 400`，错误体含 `INVALID_REQUEST` 与位置信息。
- 计数溢出经 `/restore` 置到上限后 `/tick` → `HTTP 507 LOGICAL_COUNTER_OVERFLOW`
  （由集成测试 `HLCHttpServerIT.errorHandling` 自动断言）。

## 5. 验收点对照

| 要求 | 实现/证据 | 状态 |
|---|---|---|
| Java 实现时间/版本规则后端 | `hlc.HLCClock` 等 14 个主类 | ✅ |
| JSON 输入输出 | HTTP API + CLI + 零依赖 `Json` | ✅ |
| 本地固定测试数据 | 虚拟时钟显式 `physicalMicros`；`samples/`、`DemoScenarios` | ✅ |
| 记录时区数据库版本 | tzdb=`2026b`，出现在 API 与状态文件 | ✅ |
| 不做预约/考勤系统、不做前端 | 仅 HLC 语义，无 UI、无业务系统 | ✅ |
| 发送 / 接收 | `send()` / `receive(message)` | ✅ |
| 持久化恢复 | `HLCFileStore` 临时文件+fsync+原子 rename；`restore` | ✅ |
| 物理时钟回退不破坏单调性 | `HLCClockTest.clockRegression` 等；demo 4.1/4.2 | ✅ |
| 逻辑计数溢出明确处理 | 专用异常 + HTTP 507 + 恢复路径 + 测试 | ✅ |
| 虚拟时钟 + 消息交错验证因果⇒时间戳 | `CausalityEngine` + `causalChainWithSkew` | ✅ |
| 证明不能反推时间戳⇒因果 | `timestampOrderDoesNotImplyCausality` + 样例 07 | ✅ |
| 交付源码/README/请求样例/自动化测试 | 全部就位 | ✅ |
| 实际运行并如实记录命令与结果 | 本文件 | ✅ |

## 6. 复现命令汇总

```bash
scripts/build.sh                 # 编译（主+测试）
scripts/run-tests.sh             # 35 个自动化测试
java -cp build/classes hlc.cli.Main info
java -cp build/classes hlc.cli.Main demo
java -cp build/classes hlc.cli.Main simulate samples/07-simulate-concurrent.json
scripts/demo-http.sh             # HTTP 端到端（自动用空闲端口，结束自动停服务）
```
