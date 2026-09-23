# 双流水位区间连接（Dual-Stream Interval Join with Watermark State Cleanup）

纯后端实现：Java + JDK 内置 `com.sun.net.httpserver.HttpServer`，**零第三方依赖**
（连 JSON 解析和测试框架都是手写/自带的）。提供 HTTP 接口、自动化测试、
内置验收演练（含离线全量连接对照）。

## 1. 功能与语义

### 1.1 区间连接条件

同 key 的左右事件，右事件时间 `r.ts` 落在左事件时间 `l.ts` 的指定窗口内才配对，
区间两端**闭区间**：

```
lowerBound <= r.ts - l.ts <= upperBound
```

`lowerBound` 可为负数（右事件允许早于左事件），默认窗口 `[0, 0]`（同刻连接）。

### 1.2 每对恰好输出一次

先到的事件进入状态，后到的事件只**探测对侧状态**，自己进状态时不反向探测，
因此无论两条事件以什么顺序到达、状态是否已被回收，一对事件只输出一次。

### 1.3 水位与状态回收（核心）

水位语义：`watermark = w` 断言“事件时间 **严格小于** w 的事件都已到达”。
因此 `ts < wm` 的事件按迟到丢弃；`ts == wm` 仍算准时。

**只有双方水位共同证明状态不再需要时才回收。** 定义有效水位：

```
wm = min(leftWatermark, rightWatermark)
```

- 左事件 `l` 可回收 ⇔ 未来右事件不可能再命中它。
  右流未来事件时间 `>= wm`，最大可命中时间为 `l.ts + upperBound`，
  故保留到 `l.ts + upperBound < wm`，即删除 `l.ts < wm - upperBound` 的左事件。
- 右事件 `r` 可回收 ⇔ 未来左事件不可能再命中它，
  即删除 `r.ts < wm + lowerBound` 的右事件。

推论（已被测试固化）：

- **只推进单侧水位时 `wm = -∞`（对侧从未发水位），一个事件都不会回收**；
  落后一侧随后到达的事件仍能与先到一侧的状态配对。
- 回收切点是闭边界：`l.ts + upperBound == wm` 的左事件、
  `r.ts - lowerBound == wm` 的右事件都保留（未来恰好同刻事件仍需配对）。
- 某个 key 的状态清空后，其 key 槽也被移除。

状态组织：`key -> TreeMap<ts, List<Event>>`，探测用 `subMap` 窗口二分，
回收用 `headMap` 批量删除，天然支撑同刻多事件（极端热键）。

## 2. 目录结构

```
src/main/java/intervaljoin/
  Event.java         事件模型
  Pair.java          配对输出（相等性基于 leftId/rightId）
  Stats.java         指标（accepted/lateDropped/pairsEmitted/purged…）
  JoinEngine.java    流式区间连接引擎（状态、探测、水位回收）
  OfflineJoin.java   离线全量连接参照：嵌套循环版 + 排序二分版
  DataGen.java       确定性数据生成（极端热键 + 冷键边界）
  Json.java          手写 JSON 解析/序列化
  ApiServer.java     JDK HttpServer 接口层（单线程执行器串行化）
  Main.java          入口：server 模式 / demo 验收演练
src/test/java/intervaljoin/
  TestRunner.java    零依赖测试运行器（78 个断言，含真实 HTTP 集成测试）
scripts/
  build.sh           编译到 build/classes
  test.sh            编译 + 跑全部测试
  demo.sh            编译 + 跑验收演练
  run-server.sh      编译 + 启动 HTTP 服务
dependencies.lock    依赖锁定说明（无外部构件）
```

## 3. 环境要求与启动命令

需要 **JDK 11+**（开发与验收实际使用 Temurin JDK 17.0.20）。无需 Maven/Gradle，
无需下载任何依赖。

如系统未装 JDK，可解压便携 JDK 后设置 `JAVA_HOME`（脚本会优先使用它）：

```bash
export JAVA_HOME=/path/to/jdk-17
```

编译：

```bash
scripts/build.sh
```

启动服务（默认端口 8080，可带参数）：

```bash
scripts/run-server.sh          # 端口 8080
scripts/run-server.sh 8091     # 指定端口
# 或：java -cp build/classes intervaljoin.Main server 8091
```

跑自动化测试：

```bash
scripts/test.sh
```

跑验收演练（三种场景 + 离线全量比对；参数为热键每侧事件数，默认 20000）：

```bash
scripts/demo.sh          # hotN=20000
scripts/demo.sh 1000     # 轻量版
```

## 4. HTTP 接口

所有请求/响应均为 JSON。引擎由单线程执行器驱动，事件与水位严格串行。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/config` | 设置窗口 `{"lowerBound":-2,"upperBound":3}`（仅无状态时允许） |
| POST | `/events/left` | 发左事件，单条 `{"key","ts","id"?}` 或批量 `{"events":[...]}` |
| POST | `/events/right` | 发右事件，格式同上 |
| POST | `/watermark/left` | `{"watermark":20}`，推进左流水位并回收，返回 `purgedThisCall` |
| POST | `/watermark/right` | 同上，右流 |
| GET  | `/results` | 累计全部配对 |
| GET  | `/state` | 水位、有效水位、驻留事件/key 数、全部统计 |
| POST | `/reset` | 清空状态/水位/结果；body 可选，携带 bound 时同时重配窗口 |

事件响应示例：

```json
{"received":1,"accepted":1,"lateDropped":0,"newPairs":1,
 "pairs":[{"key":"a","leftId":"L1","rightId":"R1","leftTs":10,"rightTs":8,"delta":-2}]}
```

`/state` 中的统计字段：

```
leftAccepted / rightAccepted        准时接收事件数
leftLateDropped / rightLateDropped  迟到丢弃事件数
pairsEmitted                        累计配对数
leftPurged / rightPurged            被回收事件数（题目要求的“被回收状态量”）
leftKeySlotsRemoved / rightKeySlotsRemoved  被整体清空的 key 槽数
retainedEvents / retainedKeySlots   当前驻留的事件数 / key 槽数（左右合计）
```

未发过水位的一侧在 JSON 中显示为 `null`；参数/状态错误返回 HTTP 400，
未知路由返回 404。

### 4.1 curl 请求样例

```bash
B=http://127.0.0.1:8091
curl -s $B/health

# 窗口 [-2, 3]
curl -s -X POST $B/config -H 'Content-Type: application/json' \
  -d '{"lowerBound":-2,"upperBound":3}'

# 右流批量：R1@8（delta=-2，命中下界），R2@14（delta=4，超界）
curl -s -X POST $B/events/right -H 'Content-Type: application/json' \
  -d '{"events":[{"key":"a","ts":8,"id":"R1"},{"key":"a","ts":14,"id":"R2"}]}'

# 左流：L1@10，到达即与 R1 配对
curl -s -X POST $B/events/left -H 'Content-Type: application/json' \
  -d '{"key":"a","ts":10,"id":"L1"}'

# 左水位单方面冲到 20：purgedThisCall=0，3 个事件全部驻留
curl -s -X POST $B/watermark/left -H 'Content-Type: application/json' \
  -d '{"watermark":20}'

# 右水位到 12：有效水位 min(20,12)=12，R1@8 被回收 1 条
curl -s -X POST $B/watermark/right -H 'Content-Type: application/json' \
  -d '{"watermark":12}'

# 右水位到 24：有效水位 min(20,24)=20，L1@10 与 R2@14 全部回收，驻留归 0
curl -s -X POST $B/watermark/right -H 'Content-Type: application/json' \
  -d '{"watermark":24}'

# 迟到事件（ts < 水位 20）：accepted=0, lateDropped=1
curl -s -X POST $B/events/left -H 'Content-Type: application/json' \
  -d '{"key":"a","ts":1,"id":"LATE"}'

curl -s $B/results
curl -s $B/state
curl -s -X POST $B/reset -H 'Content-Type: application/json' -d '{"lowerBound":0,"upperBound":0}'
```

## 5. 验收设计与实际运行结果

验收从三个角度构造，流式结果一律与**离线全量连接**（独立代码路径）做
无序集合比对（按 `leftId|rightId` 去重），并打印被回收状态量。

### 场景 1：两侧水位进度不均（窗口 `[-2,3]`）

左水位先冲到 20、右水位仍缺失时，回收量必须为 0，且右流迟到的 R1@8
（delta=-2 命中下界）仍能与驻留的 L1@10 配对——直接证明“单侧不能回收”。
随后右水位 12 → 24 分两步推进，逐事件验证切点：wm=12 时只回收 R1，
L1@10 保留（未来右事件 9..13 仍可能命中）；wm=20 时 L1、R2 一起回收，
key 槽移除。另验证 `ts < wm` 丢弃、`ts == wm` 保留。

### 场景 2：时间边界

delta 从 -4 到 5 逐格扫描（右先到/左后到，两个探测方向都覆盖），
只有 `-2..3` 六格命中，且与离线嵌套循环结果集合一致；
另验证同刻 3×2 事件产生 6 对、无重复，以及全负窗口 `[-5,-1]`。

### 场景 3：极端热键 + 冷键边界

热键 `hot` 左右各 20000 条密集时间戳（含伪随机分布、大量同窗口重叠，
产生 12 万+ 配对），外加 500 个冷键，每个冷键的 delta 交替取
下界、上界、区间内、区间外（含 `upperBound+1`）。41000 条事件打乱顺序
喂入流式引擎，与离线排序+二分结果、离线嵌套循环结果做三方集合比对；
结尾先单侧推极大水位（回收 0），再双侧推到极大（全部 41000 条回收、
501 个 key 槽每侧移除）。

### 实测结果（Temurin JDK 17.0.20，Linux x86_64）

`scripts/test.sh`：

```
TESTS: 78 passed, 0 failed
```

覆盖 JSON 往返、基本连接、到达顺序无关、同刻 100×100 热键无重复、
闭区间边界、单侧水位不回收、回收切点恰好性、迟到语义、配置保护、
2000 条随机数据三方一致、10000 热键大数据、以及真实启动 HTTP 服务的
接口集成测试（含 400/404）。

`scripts/demo.sh 20000`：

```
断言汇总：21 passed, 0 failed
DEMO RESULT: PASS
场景3: 事件总数=41000 配对总数=120366
       流式耗时≈150ms 离线嵌套循环耗时≈4.7–5.6s
       回收左事件=20500 回收右事件=20500
       移除左key槽=501 移除右key槽=501
       收尾前驻留事件=41000 -> 双侧水位后 0
```

流式引擎与离线全量连接输出集合完全相等；单侧推极大水位回收 0、
双侧推极大后驻留归零，回收总数 = 接收事件总数。

HTTP 接口已用 curl 实际调用验证（上方样例的真实响应）：
单条/批量、边界配对、单侧零回收、双侧分步回收、迟到计数、
水位回退 400、缺字段 400、未知路由 404、reset 清零均符合预期。

## 6. 依赖

零第三方依赖，详见 `dependencies.lock`：

- `java.base`、`java.net.http`（测试客户端，JDK 11+）、
  `jdk.httpserver`（内置 HttpServer）。
- 无 Maven/Gradle 构建文件，`javac` 直接编译；无外部构件需要锁定版本。

## 7. 已知限制 / 未完成项

如实说明：

1. **无鉴权、无持久化**：进程重启后状态与结果丢失；`/reset` 可清空。
   定位为本地/内网验收服务，未加任何访问控制与 TLS。
2. **单线程执行器**：所有请求串行处理，保证语义确定但不追求吞吐；
   高并发多分区需要按 key 分片改造。
3. **背压/有界状态未实现**：水位落后时状态只增不减（这是正确性要求），
   没有配置状态 TTL、允许迟到时间或状态上限；极端情况下内存随唯一 key
   数增长。
4. **水位由外部显式推送**，不根据已见事件时间自动推导；
   多分区取 min 的合并逻辑由调用方负责。
5. 结果通过 `/results` 在内存中全量保留，超大输出下应改为流式拉取/分页，
   当前未实现。
6. 事件时间为 64 位整数毫秒语义；未支持事件时间属性自定义。
