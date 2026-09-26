# hlc-backend — 逻辑混合时钟（Hybrid Logical Clock）纯后端

用 Java 21 实现的 Hybrid Logical Clock（HLC）时间/版本规则计算后端。提供 JSON
输入输出的 HTTP 接口，支持发送（send）、接收（receive）、持久化恢复，物理时钟
回退不破坏单调性，逻辑计数溢出有明确处理策略。无前端、无预约/考勤业务逻辑。

## 背景与规则

HLC 时间戳为三元组 `(physicalMillis, logical, nodeId)`：

- **send / 本地事件（tick）**：若墙钟前进则取 `physical = wallNow, logical = 0`；
  否则（墙钟停滞或回退）`physical` 保持不变、`logical + 1`——物理时钟回退
  不会把 HLC 物理分量往回拉，单调性由逻辑计数保证。
- **receive（合并远端时间戳 m）**：
  - `wallNow > lastPhysical 且 wallNow > m.physical` → `(wallNow, 0)`
  - `lastPhysical == m.physical` → `logical = max(logical, m.logical) + 1`
  - `m.physical > lastPhysical` → `(m.physical, m.logical + 1)`
  - 否则 → `logical + 1`
- **排序**：按 `(physicalMillis, logical, nodeId)` 字典序，构成全序。
- **因果保证**：若事件 a happens-before b，则 `ts(a) < ts(b)`。
  **反之不成立**：时间戳有序 ≠ 有因果关系（并发事件的时间戳同样全序可比较）。
  验收测试 `CausalityInterleavingTest` 对两个方向分别做了正向验证与反例演示。

## 逻辑计数溢出（明确处理）

逻辑计数上限 `maxLogical` 可配置（默认 4095，12 bit）。同一物理毫秒内计数
超限时有两种显式策略（`--overflow` 选择）：

| 策略 | 行为 |
|---|---|
| `BUMP_PHYSICAL`（默认） | 物理分量 +1ms（短暂超前于墙钟），逻辑计数归零，单调性保持 |
| `THROW` | 抛出 `LogicalOverflowException`，HTTP 层返回 409，调用方待墙钟前进后重试 |

另外可配置 `--max-drift-ms`：远端时间戳物理分量超前本地墙钟超过该阈值时拒绝
合并（HTTP 422），防止异常节点把本地时钟无限推向未来；默认 0 表示不启用。

## 持久化恢复

每次 send/receive 后状态以 JSON 原子写入（临时文件 + atomic move）状态文件。
重启时加载已持久化状态作为 HLC 初值：即使墙钟回退到持久化物理时间之前，
下一个时间戳仍严格大于已持久化值（见 `PersistenceTest`）。

## 时区数据库版本

`GET /api/meta` 返回运行时元数据，其中 `tzdbVersion` 来自 JVM 内置
`java.time.zone.ZoneRulesProvider`（本机实测为 `2026b`），同时报告
`systemZone`、`javaVersion`、节点与溢出策略配置。

## 构建与运行

```bash
mvn test          # 运行全部自动化测试
mvn package       # 生成 target/hlc-backend-1.0.0.jar（含依赖的可执行 jar）

java -jar target/hlc-backend-1.0.0.jar \
    --port 8080 --node node-1 \
    --state-file data/hlc-state.json \
    --max-logical 4095 --overflow BUMP_PHYSICAL --max-drift-ms 0
```

## HTTP 接口（JSON 输入输出）

统一响应信封：成功 `{"ok": true, "data": ...}`，失败 `{"ok": false, "error": "..."}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 运行时元数据（含 tzdb 版本） |
| GET | `/api/now` | 当前时间戳，不推进时钟 |
| POST | `/api/send` | 本地/发送事件，返回新时间戳并持久化 |
| POST | `/api/receive` | 合并远端时间戳，返回本地新时间戳并持久化 |

错误码：400 请求体非法 / 缺字段；409 逻辑溢出（THROW 策略）；405 方法错误；
422 远端时间戳漂移超限。

### 请求样例

样例文件见 `samples/`（`send-request.json`、`receive-request.json`），
一键脚本 `samples/curl-examples.sh`：

```bash
curl -sS http://localhost:8080/api/meta
curl -sS -X POST http://localhost:8080/api/send \
     -H 'Content-Type: application/json' -d @samples/send-request.json
curl -sS -X POST http://localhost:8080/api/receive \
     -H 'Content-Type: application/json' -d @samples/receive-request.json
```

`receive` 请求体：

```json
{
  "timestamp": {
    "physicalMillis": 1727000005000,
    "logical": 3,
    "nodeId": "remote-node"
  }
}
```

## 自动化测试（验收）

- `HybridLogicalClockTest` — 物理时钟回退 60s 后连续 100 次 tick 仍严格单调；
  receive 三种合并分支；两种溢出策略；漂移超限拒绝。
- `CausalityInterleavingTest` — **验收核心**：虚拟时钟 + 三节点固定消息交错
  （含中途物理时钟回退），验证全部因果对满足 `a→b ⇒ ts(a)<ts(b)`；并用并发
  事件 `a1`、`c1` 证明 `ts(a1)<ts(c1)` 不能反推因果。另有 5 个固定种子的
  随机交错场景（本地固定测试数据，可复现）做不变量全量校验。
- `PersistenceTest` — 状态保存/加载回读；重启且墙钟回退 50s 后仍单调；
  状态文件损坏时降级为空状态。
- `HlcServerTest` — 真实 HTTP 往返：send/receive/meta/now、畸形请求 400。
- `HlcTimestampTest` — 排序全序性与 JSON 序列化往返。

## 实际运行记录（如实记录）

环境：OpenJDK 21.0.12.1，Apache Maven 3.8.7，Linux 6.8.0-90-generic。

### `mvn test`（2026-09-25 实际执行）

```text
Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 -- CausalityInterleavingTest
Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 -- PersistenceTest
Tests run: 3, Failures: 0, Errors: 0, Skipped: 0 -- HlcTimestampTest
Tests run: 5, Failures: 0, Errors: 0, Skipped: 0 -- HlcServerTest
Tests run: 8, Failures: 0, Errors: 0, Skipped: 0 -- HybridLogicalClockTest
Tests run: 22, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

测试过程输出：

```text
verified 54 causal pairs over 13 events
randomized scenarios verified 6353 causal pairs, 0 violations
tzdbVersion=2026b systemZone=Asia/Shanghai
```

### 服务实测（`java -jar ... --port 18080 --node demo-node --max-drift-ms 300000`）

```text
== GET /api/meta ==
{"ok":true,"data":{"nodeId":"demo-node","javaVersion":"21.0.12.1","tzdbVersion":"2026b","systemZone":"Asia/Shanghai","maxLogical":4095,"overflowPolicy":"BUMP_PHYSICAL","maxDriftMillis":300000}}
== POST /api/send ==
{"ok":true,"data":{"physicalMillis":1790319211606,"logical":0,"nodeId":"demo-node"}}
== POST /api/receive（样例中远端物理时间早于本地，正确保持本地物理分量） ==
{"ok":true,"data":{"physicalMillis":1790319211667,"logical":0,"nodeId":"demo-node"}}
== POST /api/receive（畸形请求体） ==
{"ok":false,"error":"request body is not valid JSON: Unrecognized token 'not': ..."}
== 状态文件 /tmp/hlc-demo/state.json ==
{"physicalMillis":1790319211667,"logical":0,"nodeId":"demo-node"}
```

重启恢复实测（第二次启动加载同一状态文件）：

```text
HLC backend listening on http://localhost:18081 node=demo-node ... restored=HLC{physical=1790319211667, logical=0, node=demo-node}
== GET /api/now（重启后立即可见已恢复状态） ==
{"ok":true,"data":{"physicalMillis":1790319211667,"logical":0,"nodeId":"demo-node"}}
== POST /api/send（恢复后继续单调推进） ==
{"ok":true,"data":{"physicalMillis":1790319418838,"logical":0,"nodeId":"demo-node"}}
```

**未通过项：无。** 22 个测试全部通过；服务各端点实测均符合预期。

## 项目结构

```text
src/main/java/com/example/hlc/
├── Main.java                     # 入口：参数解析、装配、启动
├── clock/                        # 物理时钟抽象（System / Virtual）
├── core/                         # HlcTimestamp、HybridLogicalClock、溢出与漂移异常
├── meta/TzdbInfo.java            # 时区数据库版本
├── persist/                      # 状态存储接口与 JSON 文件实现（原子写）
└── server/HlcServer.java         # JDK HttpServer JSON 接口层
src/test/java/com/example/hlc/    # 5 个测试类 + sim/HlcSimulation（交错模拟器）
samples/                          # 请求样例 JSON 与 curl 脚本
```
