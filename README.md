# 可撤回精确 TopK（分组滑动窗口）

纯后端服务：按分组（group）维护滑动时间窗口内的精确 TopK 排名，支持事件插入与撤回。
仅使用 **JDK 内置 `com.sun.net.httpserver`**，零第三方依赖，无界面，只提供 HTTP 接口。

## 语义定义

- **事件** = `(eventId, group, key, delta, ts)`：`delta` 可正可负（负增量合法）。
- 元素 `key` 的分数 = 窗口内所有活跃事件的 `delta` 之和；**净分为 0 时元素从排名中移除**。
- **排名**：分数降序；分数相同按 `key` 字典序升序。
- **窗口**：事件在 `ts > watermark - windowMs` 时活跃（下界开区间）。
  `watermark` 是单调递增的逻辑时间，由事件 `ts`、查询参数 `now`、`POST /advance` 共同推进。
- **过期与撤回各自只扣减一次**：事件一旦过期或被撤回即从活跃集合移除；
  过期堆中的残留条目弹出时发现已不活跃则直接跳过，不会二次扣分。
- **不保存全部历史再重算**：服务只增量维护每分组的 `key→score` 哈希表、
  有序排名 `TreeSet` 和按 `ts` 排序的活跃事件堆，每次插入/撤回/过期都是 O(log n) 增量更新。
  （测试中的 Oracle 参照实现才用重算法，用于逐事件对照。）
- `eventId` 全局一次性：重复插入返回 409；撤回后同 id 也不能复用。
- 撤回幂等：撤回不存在 / 已撤回 / 已过期的事件返回 `not_active`，不报错、不重复扣减。

## 依赖（锁定）

| 依赖 | 版本 | 说明 |
|---|---|---|
| JDK | 17+（实测 Temurin 21.0.12.1+1） | 唯一依赖 |
| 第三方库 | 无 | JSON 解析/序列化为自带极简实现（`src/topk/Json.java`） |

零第三方依赖，因此没有需要锁定的外部构件；锁定项即 JDK 版本。
本机若无 JDK，可将任意 JDK 17+ 解压到 `~/jdk`，脚本会自动使用。

## 启动

```bash
./run_server.sh [port] [windowMs]
# 默认 port=8080, windowMs=60000；也可用环境变量 PORT / WINDOW_MS
```

## 测试

```bash
./run_tests.sh   # 编译 src/ 与 test/，运行全部测试，失败即非零退出
```

## 请求样例

```bash
./run_server.sh 18080 1000 &   # 窗口 1000ms
BASE=http://127.0.0.1:18080 ./examples.sh
```

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/events` | 插入事件 `{"eventId":"e1","group":"g1","key":"apple","delta":5,"ts":1000}` |
| POST | `/retract` | 撤回事件 `{"eventId":"e1"}` |
| POST | `/advance` | 推进逻辑时间 `{"now":5000}` |
| GET | `/topk?group=g1&k=3&now=2000` | 精确 TopK（`now` 可选，缺省不推进时间） |
| GET | `/ranking?group=g1&now=2000` | 窗口内完整排序 |
| GET | `/health` | 健康检查 |

```bash
curl -X POST localhost:8080/events -d '{"eventId":"e1","group":"g1","key":"apple","delta":5,"ts":1000}'
# {"status":"applied","watermark":1000}

curl "localhost:8080/topk?group=g1&k=10&now=1500"
# {"group":"g1","k":10,"windowMs":1000,"watermark":1500,"count":1,
#  "items":[{"rank":1,"key":"apple","score":5}]}
```

## 实测记录（2026-09-23，Linux x86_64）

`./run_tests.sh` 实际运行结果——**全部通过，进程正常退出（exit 0）**。
分别在 Temurin JDK 21.0.12.1 与 OpenJDK 17.0.20.1 上验证通过：

```
PASS duplicateEventIdRejected      # 重复 eventId 拒绝
PASS groupsAreIsolated             # 分组隔离
PASS kLargerThanElementCount       # K 大于元素数返回全部
PASS lateEventDropped              # 迟到事件插入即过期
PASS negativeDeltas                # 负增量、负分参与排名、净分 0 移除
PASS randomizedReplayAgainstOracle # 3 个随机种子 × 3000 步，逐事件对照完整排序 + TopK 前缀
PASS retractRestoresTieOrder       # 撤回后并列顺序恢复
PASS retractThenExpireDeductsOnce  # 先撤回后过期只扣一次
PASS tiedRanksBrokenByKey          # 并列名次按 key 升序
PASS windowSlideOut                # 窗口滑出只扣一次，过期后撤回为空操作
PASS HttpApiTest.* (6 groups)      # 真实 HTTP 集成：插入/查询/409/撤回/滑出/400·404
ALL TESTS PASSED
```

`./examples.sh` 对运行中的服务实际执行，关键输出（与预期逐条核对一致）：

- 并列：`banana=5, apple=5`（apple 另有 -2 负增量后为 3）→ 排名 `cherry(9), banana(5), apple(3)`；
- K=10 大于元素数 3 → 返回全部 3 个元素；
- 撤回 `e3` 后 cherry 消失；重复撤回返回 `not_active`；
- `advance` 到 2500 后窗口滑出，排名为空；过期后再撤回 `e1` 仍为空（未二次扣减）；
- 重复 `eventId` 返回 HTTP 409。

## 设计决策与未完成项

- **逻辑时间**：时间由客户端通过 `ts` / `now` / `/advance` 显式推进，便于确定性测试；
  未实现墙钟自动过期（生产部署可在查询时注入 `System.currentTimeMillis()` 作为 `now`）。
- **单进程内存态**：无持久化、无副本；重启即丢失。
- **净分为 0 移除元素**：显式插入 `delta=0` 的事件不会让元素出现在排名中。
- **无鉴权、无流控**：仅面向内网/测试场景。
- 过期按分组惰性触发（访问该分组时才清理），不影响查询结果正确性。
