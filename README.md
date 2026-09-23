# 事件时间滚动窗口计数服务（event-time tumbling window）

纯后端 HTTP 服务：接收**分区事件**与**显式水位线**，按事件时间做**滚动窗口（tumbling window）计数**。
仅使用 JDK 内置 `com.sun.net.httpserver.HttpServer`，**零第三方依赖**（JSON 解析器也是手写的，
见 `src/main/java/com/example/window/Json.java`）。

## 语义定义（精确）

设窗口大小 `windowSize = W`，迟到容忍期 `allowedLateness = L`（启动参数，单位与事件时间一致，
为抽象长整型时间戳，不含真实时钟语义）。

- **窗口分配**：`start = floor(eventTime / W) * W`，窗口为**左闭右开** `[start, start+W)`。
  因此 `eventTime = 10` 在 `W = 10` 时属于 `[10,20)`，不属于 `[0,10)`；负数时间同样按 floor 语义
  （`ts = -1 → [-10, 0)`）。
- **分区水位线**：每个分区维护一条显式水位线，**单调不减**；上报更小的水位线返回 400。
- **全局水位线**：所有**活跃**分区水位线的**最小值**，且只增不减。没有任何活跃分区时为
  `null`（内部为 `Long.MIN_VALUE`）。
- **窗口关闭（发射）**：当 `全局水位线 >= 窗口末端` 且计数较上次发射有变化时发射一条结果，
  记录 `closedAtWatermark`（即关闭时的全局水位线）。容忍期内的迟到事件会更新计数，
  并在水位线再次推进时**重发**更新后的结果。
- **窗口清除（purge）**：当 `全局水位线 >= 窗口末端 + L` 时清除窗口状态（含去重集合），
  该窗口最近一次发射被标记 `"final": true`，并记录一条 purge 日志（含 `finalCount`）。
- **侧输出**：窗口已清除后到达的事件写入侧输出（`status = LATE_SIDE_OUTPUT`），不再计数。
- **去重**：同一窗口内按事件 `id` 去重，重复事件返回 `DUPLICATE`、丢弃并计数。
- **空闲分区**：`POST /api/partitions/idle` 将分区标记为空闲，退出全局水位最小值计算；
  该分区再次上报事件或水位线时**自动恢复**为活跃。全部分区空闲时全局水位保持不动（单调性）。
- **确定性**：服务端单线程处理请求，同一输入序列必然产生同一输出序列。

## 依赖（锁定）

| 项 | 版本 | 说明 |
|---|---|---|
| JDK | Temurin OpenJDK **17.0.20.1+1** | 开发/测试锁定版本；源码兼容 JDK 11+ |
| 第三方库 | **无** | HTTP、JSON、测试框架全部基于 JDK 自带能力 |

本机 JDK 安装在 `~/tools/jdk-17.0.20.1+1`（`scripts/resolve-jdk.sh` 按
`$JAVA_HOME` → `PATH` → `~/tools/jdk-17.0.20.1+1` → `~/tools/jdk-*` 顺序探测）。
复现环境可用：`curl -L -o jdk.tar.gz "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"`。

## 构建、启动、测试

```bash
./build.sh                                            # 编译到 build/classes
./run.sh --port 8080 --window-size 10 --allowed-lateness 2   # 启动服务
./test.sh                                             # 编译并运行全部测试（118 项断言）
PORT=18099 bash examples/demo.sh                      # 一键 HTTP 演示（自动起停服务）
```

## HTTP API

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/api/events` | 事件对象或数组 | 逐条返回 `ACCEPTED` / `DUPLICATE` / `LATE_SIDE_OUTPUT` |
| POST | `/api/watermark` | `{"partition":"p1","watermark":12}` | 推进分区水位线；回退返回 400 |
| POST | `/api/partitions/idle` | `{"partition":"p1"}` | 标记分区空闲 |
| GET | `/api/state` | — | 全量快照（水位线、开放窗口、结果、侧输出、purge、统计） |
| GET | `/api/results` | — | 全部窗口发射记录 |
| GET | `/api/side-output` | — | 侧输出记录 |
| POST | `/api/reset` | — | 清空全部状态 |
| GET | `/` | — | 端点说明 |

事件字段：`id`（字符串，窗口内去重键）、`key`（计数分组键）、`eventTime`（整数）、
`partition`（字符串）。错误响应：400（JSON 非法/字段缺失/水位线回退）、404、405。

请求样例见 [examples/requests.md](examples/requests.md)。

## 确定性验收序列（windowSize=10, allowedLateness=2）

自动化测试 `WindowEngineTest.deterministicSequence` 与 `examples/demo.sh` 使用同一序列：

| # | 输入 | 期望 |
|---|---|---|
| 1 | e1(p1,ts=3), e2(p2,7), e3(p1,9), e10(p2,0) | 全部 ACCEPTED 进 `[0,10)`，无窗口关闭 |
| 2 | e7(p1,10), e8(p2,19) | 边界：ts=10 进 `[10,20)` |
| 3 | wm p1=10 | 全局水位仍 null（p2 未上报，拖住最小值） |
| 4 | wm p2=10 | 全局=10；`[0,10)` 关闭，count=4，closedAt=10 |
| 5 | e4(p1,5) 迟到 | ACCEPTED（10 < 10+2，容忍期内） |
| 6 | e4 重发 | DUPLICATE，计数不变 |
| 7 | wm p1=11 | 全局仍 10（p2=10） |
| 8 | wm p2=11 | 全局=11；`[0,10)` 重发，count=5 |
| 9 | wm p1=12, p2=12 | 全局=12 ≥ 10+2：`[0,10)` purge，finalCount=5 |
| 10 | e6(p1,6) | LATE_SIDE_OUTPUT，进侧输出 |
| 11 | idle p1；wm p2=25 | 全局=25：`[10,20)` 关闭并直接 purge，count=2 |
| 12 | e11(p1,26) | p1 自动恢复；全局仍 25（min(12,25)） |
| 13 | wm p1=30, p2=30 | 全局=30：`[20,30)` 关闭，count=1，未 purge（30<32） |

最终：`accepted=8, duplicates=1, sideOutput=1`，purge 记录两条
（`[0,10)@12 finalCount=5`、`[10,20)@25 finalCount=2`）。

## 实际运行记录（2026-09-23，本机 Linux x86_64）

`./test.sh`：

```
using javac: /home/admin/tools/jdk-17.0.20.1+1/bin/javac
javac 17.0.20.1
build OK -> build/classes
---- running tests ----
JsonTest            15 项 ok
WindowEngineTest    75 项 ok
HttpApiTest         28 项 ok
TOTAL passed: 118, failed: 0
```

`PORT=18099 bash examples/demo.sh` 关键输出（完整输出见运行日志）：

```
== 4. watermark p2=10: window [0,10) closes, count=4
{"partition":"p2","partitionWatermark":10,"globalWatermark":10}

== 5. emitted results
{"windowStart":0,"windowEnd":10,"count":4,"byKey":{"a":2,"b":1,"c":1},
 "closedAtWatermark":10,"final":false}

== 10. e6 ts=6 after purge -> side output
{"eventId":"e6","windowStart":0,"windowEnd":10,"status":"LATE_SIDE_OUTPUT","globalWatermark":12}

== 15. final state snapshot（节选）
"purges":[
  {"windowStart":0,"windowEnd":10,"purgedAtWatermark":12,"finalCount":5},
  {"windowStart":10,"windowEnd":20,"purgedAtWatermark":25,"finalCount":2}],
"stats":{"accepted":8,"duplicates":1,"lateSideOutput":1}
```

与上表期望逐项一致。

## 已知限制 / 未完成项

- **单实例内存态**：无持久化、无副本、无分布式；重启即丢失（提供 `/api/reset` 手动清空）。
- **去重范围**：去重集合随窗口 purge 一并清除；purge 后同 id 事件无法识别为重复，
  按“窗口已清除”进入侧输出。
- **无真实时钟**：不处理 processing time、无定时器，窗口推进完全由输入水位线驱动。
- **JSON 解析器为极简实现**：覆盖本 API 所需类型（对象/数组/字符串/数字/布尔/null、
  `\uXXXX` 转义），未做 UTF-16 surrogate pair 合成、大数溢出保护等完整校验。
- **溢出**：`eventTime`、`watermark` 为 `long`，`windowEnd + allowedLateness` 在极端
  取值下理论上可能溢出，未做防护（正常时间戳范围不受影响）。
- 首次演示时发现默认端口被本机其他服务占用，已将 demo 默认端口改为 18091 并增加
  启动失败检测；`PORT` 环境变量可覆盖。
