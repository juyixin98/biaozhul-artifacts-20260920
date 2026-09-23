# 双流水位区间连接（Dual-Stream Watermark Interval Join）

纯 Java 后端服务：两条事件流（左/右）按 **同 key + 事件时间区间** 配对，
基于**双侧水位（watermark）**回收状态。零第三方依赖，仅用 JDK 内置
`com.sun.net.httpserver.HttpServer` 提供 HTTP/JSON 接口。

---

## 1. 语义定义

- 配对谓词（**闭区间，含边界**）：

  ```
  left.key == right.key 且  lowerBound <= right.ts - left.ts <= upperBound
  ```

  区间在创建会话时指定（示例用 `[-2, +2]`）。
- 每个事件通过 `id` 唯一标识；同一对 `(leftId, rightId)` **只输出一次**（去重）。
- **水位语义**：水位 `t` 表示“事件时间 ≤ t 的事件都已到达”。因此
  `eventTime <= 当前同侧水位` 的事件判为**迟到**，直接丢弃（计入 `*DroppedLate`），
  不进状态、不产生配对。水位单调前进，回退/重复推进无效。
- 乱序到达安全：后到的事件会与对侧已缓存状态做区间匹配，因此两侧任意注入顺序结果一致。

### 状态回收：必须由双方水位共同证明

设左右水位为 `wl`、`wr`（未到达 = -∞）：

| 状态 | 可回收条件 | 截断阈值 |
|---|---|---|
| 左事件 `l.ts` | `wl >= l.ts` 且 `wr >= l.ts + upperBound` | `cutL = min(wl, wr - upperBound)`，回收 `l.ts <= cutL` |
| 右事件 `r.ts` | `wr >= r.ts` 且 `wl >= r.ts - lowerBound` | `cutR = min(wr, wl - lowerBound)`，回收 `r.ts <= cutR` |

含义：单侧水位再高也不能回收——对侧迟到的可配对事件还可能到达。
只有“本侧水位封死迟到可能”**且**“对侧水位把时间窗推出可配对范围”同时成立才回收。
任一侧水位推进都会同时检查两侧状态（`JoinSession.java`）。

复杂度：每 key 维护按事件时间排序的 `TreeMap`，单次事件连接为
`O(log n + k)`（k=命中事件数），水位回收为前缀删除。

---

## 2. 目录结构

```
src/ij/            主程序源码
  Main.java            HTTP 服务入口
  ApiServer.java       路由与 JSON over HTTP（JDK HttpServer）
  SessionRegistry.java 会话注册表（多会话隔离）
  JoinSession.java     核心引擎：连接、去重、双侧水位回收
  Event/Pair/EventResult.java  数据模型
  Json.java            零依赖 JSON 解析/序列化
test/ij/           自动化测试（自带轻量断言，无需 JUnit）
  TestRunner.java      测试入口（失败退出码 1）
  JsonTest / JoinSessionTest / ApiServerTest
  AcceptanceTest.java  验收：乱序+水位不均+边界+热键，对比离线全量连接
  OfflineJoin.java     离线全量连接参考实现（独立 O(n·m) 基准）
scripts/           build.sh / test.sh / run.sh / demo.sh / find-java.sh
examples/          HTTP 请求 JSON 样例
docs/              实际测试与 demo 运行记录
DEPS.lock.md       依赖锁定（零第三方依赖 + JDK 版本/校验和）
```

---

## 3. 环境与启动

要求 **JDK 11+**（本机实测 JDK 17，见 `DEPS.lock.md`）。无任何外部依赖需要下载。

```bash
# 编译
bash scripts/build.sh

# 跑全部自动化测试（编译主程序+测试并执行，失败返回非零）
bash scripts/test.sh

# 启动 HTTP 服务（默认 127.0.0.1:8080）
bash scripts/run.sh                 # 或：bash scripts/run.sh 9090
PORT=9090 BIND=0.0.0.0 bash scripts/run.sh   # 自定义端口/对外监听

# 一键端到端演示（自动起服务、造数、推进水位、展示回收量后关停）
bash scripts/demo.sh
```

若 `java` 不在 PATH，设置 `JAVA_HOME=/path/to/jdk` 即可（`scripts/find-java.sh`
也会自动探测 `/home/admin/tools/jdk-17*` 等位置）。

---

## 4. HTTP 接口

所有请求/响应均为 `application/json; charset=utf-8`。事件时间戳用整数（毫秒/秒皆可，语义自定）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| POST | `/sessions` | 创建会话：`{"sessionId","lowerBound"?, "upperBound"?}`（默认 [-2,2]），重复返回 409 |
| GET  | `/sessions` | 会话 id 列表 |
| GET  | `/sessions/{id}` / `/sessions/{id}/stats` | 状态与计数（水位、现存状态量、**累计回收量**、丢弃量、配对数） |
| DELETE | `/sessions/{id}` | 删除会话 |
| POST | `/sessions/{id}/events/left` | 接入左事件，单对象或数组 |
| POST | `/sessions/{id}/events/right` | 接入右事件，单对象或数组 |
| POST | `/sessions/{id}/watermarks/left` | `{"watermark": 100}` 推进左水位，返回本次回收数 |
| POST | `/sessions/{id}/watermarks/right` | 同上，右水位 |
| GET  | `/sessions/{id}/pairs?includePayload=true&key=u` | 查询全部配对（可按 key 过滤） |
| POST | `/sessions/{id}/actions` | 顺序编排一批 `event`/`watermark` 动作，逐步返回新增配对与回收量 |

事件对象：`{"id":"L1","key":"u","ts":5,"payload":{...}}`
（`id` 可省略，服务端自动生成；`payload` 不透明存储，仅在 `includePayload=true` 时回显。）

### curl 请求样例

```bash
BASE=http://127.0.0.1:8080

curl -s -XPOST $BASE/sessions -H 'Content-Type: application/json' \
  -d '{"sessionId":"demo","lowerBound":-2,"upperBound":2}'

curl -s -XPOST $BASE/sessions/demo/events/left  -H 'Content-Type: application/json' \
  -d @examples/02-left-events.json
curl -s -XPOST $BASE/sessions/demo/events/right -H 'Content-Type: application/json' \
  -d @examples/03-right-events.json

curl -s "$BASE/sessions/demo/pairs?includePayload=true"

# 单侧水位：reclaimedThisCall 必须为 0（对侧水位未证明）
curl -s -XPOST $BASE/sessions/demo/watermarks/left  -H 'Content-Type: application/json' -d '{"watermark":100}'
# 双侧齐备：本次回收全部可回收状态，stats 给出 leftReclaimed/rightReclaimed/totalReclaimed
curl -s -XPOST $BASE/sessions/demo/watermarks/right -H 'Content-Type: application/json' -d '{"watermark":100}'

curl -s $BASE/sessions/demo/stats
```

### stats 字段

`watermarkLeft/Right`（未推进为 null）、`left/rightReceived`、`left/rightDroppedLate`、
`left/rightStateEvents`（当前留存事件数）、`left/rightStateKeys`（当前留存 key 数）、
`left/rightReclaimed` 与 **`totalReclaimed`（被回收状态量，累计）**、`pairsEmitted`。

---

## 5. 验收场景与实际结果（2026-09-23 实跑）

运行记录原文见 `docs/test-output.txt` 与 `docs/demo-output.txt`。

### 5.1 自动化测试：`bash scripts/test.sh`

```
== JsonTest            （JSON 解析/序列化/拒绝非法输入）
== JoinSessionTest     （闭区间边界、双向到达顺序、迟到、双方水位回收、负区间、去重、多 key）
== AcceptanceTest      （乱序 + 水位不均 + 时间边界 + 800 热键，逐场景对比离线全量连接）
   [info] hot key: n=800, pairs=1600, offline cross-check=39ms
== ApiServerTest       （真实启动 HTTP 服务的端到端用例）

checks: 140, failures: 0
RESULT: PASS
```

离线对比方式（`AcceptanceTest` + `OfflineJoin`）：
- 离线参考实现独立于流式引擎，对两侧**完整事件集**做同 key 全量匹配并排序；
- 40 组随机 fuzz（随机区间 [-2..+5]、1~4 个 key、每 key 1~12 条两侧事件、洗牌乱序注入）
  逐组断言流式配对集合 == 离线全量配对集合；
- 固定场景断言含闭区间边界（r-l 恰为 lower/upper 必须命中）与同时间多事件（2 个 L@5 各自配对）。

### 5.2 两侧水位进度不均

demo 中 7 左 + 8 右事件（区间 [-2,2]）：
- 只推进左水位到 100：`reclaimedThisCall=0`，`leftStateEvents=7, rightStateEvents=8`
  —— 单侧水位证明不了任何状态“不再需要”；
- 再推进右水位到 100：`reclaimedThisCall=15`，留存状态归零，
  `leftReclaimed=7, rightReclaimed=8, totalReclaimed=15`。

### 5.3 时间边界

- 单元测试：区间 `[0,0]` 仅同时间命中；`[1,3]` 时差恰为 1 和 3 均命中、为 4 不命中。
- 确定性 fuzz 场景：R@3（l-2 下界）、R@7（l+2 上界）命中，R@8 超界不命中。

### 5.4 极端热键

单 key `HOT`：左 800 条（ts 0..799）、右 1600 条（每时间点 2 条，逆序注入），
区间 `[0,0]` → 输出 **1600 对**，与离线全量连接结果集合完全一致；
单侧水位回收 0，双侧水位后 **3n=2400** 个状态事件全部回收、空 key 桶移除。
引擎按 TreeMap 索引做范围查询，热键下不会随无关事件增长而退化为全表扫描。

### 5.5 迟到与去重

- 双侧水位 100 后注入 `ts=1` 的左事件：`droppedLate=true`、`leftDroppedLate=1`、不产生配对；
- 相同 `id` 的事件重复接入不会重复输出配对（`JoinSessionTest.idDedup`）。

---

## 6. 设计取舍 / 限制 / 未完成项

- **会话状态在内存中**：进程重启丢失；没有持久化与 checkpoint（题目未要求）。
- **已输出配对全量留存**：`/pairs` 可随时查询全部历史配对。超大流量下配对结果集会持续增长；
  当前只对“连接状态”做水位回收，没有对结果集做分页滚动/过期（接口支持按 key 过滤，但不做服务端游标）。
- **单实例、无鉴权、无 TLS**：默认仅绑定 127.0.0.1；HTTP 服务串行处理（单线程 executor），
  适合演示与验收，不是高吞吐生产部署。引擎本身所有方法 synchronized，换成多线程 executor 也安全。
- **水位由外部驱动**：服务不自己从事件生成水位，由调用方按其语义 POST（周期性或 punctuation 均可）。
- **不做 out-of-order 缓冲窗口外的“补结果撤回”**：迟到事件直接丢弃，不发撤回/更新流。
- 时间戳为 64 位整数；区间端点运算有溢出钳制，但未在 long 边界附近做专门测试。
- JSON 为自带最小实现（覆盖对象/数组/字符串/数字/bool/null），不支持注释与流式解析。
