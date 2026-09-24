# 流式去重期限服务（stream-dedup-ttl）

纯后端事件 ID 去重服务。仅使用 JDK 内置类库（`com.sun.net.httpserver` + `java.net.http`），
**零第三方依赖**。保留期按事件时间与水位（watermark）定义；分区迁移时去重状态随键转移，
携带错误路由版本的请求被拒绝。

## 依赖与锁定

| 项 | 版本 | 说明 |
|---|---|---|
| JDK | 17（`.java-version` 锁定，编译用 `javac --release 17`） | 唯一依赖 |
| 第三方库 | 无 | JSON 解析/序列化为内置的最小实现（`Json.java`） |

本仓库在 OpenJDK 17.0.20.1 上构建与验证。

## 语义

- 事件 = `(eventId, eventTime)`，按 `hash(eventId)` 分入固定数量分区。
- 每个分区维护水位：`watermark = maxEventTime - allowedLatenessMs`，也可通过
  `POST /watermark` 显式推进（只前进，不后退）。
- `eventId` 在 `eventTime + retentionMs > watermark` 期间被记住；窗口内同 ID（含乱序、
  更早时间戳的重投）判为 `duplicate`。
- 当 `watermark >= eventTime + retentionMs` 时条目被淘汰（内存随水位推进释放），
  此后同 ID 视为**新事件**。
- 每个变更请求携带 `routingVersion`，与服务当前版本不一致即拒绝（HTTP 409）。
- 迁移：`bump` 升版本 → `export` 导出分区快照（含水位与全部条目）→ 新节点 `import`。
  快照版本低于目标节点当前版本时拒绝导入；导入后两边版本一致，旧版本请求双侧均被拒。

## 启动

```bash
./run-server.sh                                        # 默认 :8080, 16 分区, lateness 60s, retention 300s
./run-server.sh --port=8080 --partitions=4 --allowed-lateness-ms=0 --retention-ms=500
```

## 测试

```bash
./run-tests.sh   # 编译 + 运行全部测试，任一失败退出码非 0
```

## HTTP 接口

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| GET | `/health` | — | `{"status":"ok"}` |
| GET | `/state` | — | 各分区水位与条目数、当前路由版本 |
| POST | `/events` | `{"eventId","eventTime","routingVersion"}` | 200 `{"status":"new"\|"duplicate","partition","watermark"}`；409 版本过期；400 参数错误 |
| POST | `/watermark` | `{"partition","watermark","routingVersion"}` | 200 `{"evicted":N}` |
| POST | `/migration/bump` | `{}` | 200 `{"routingVersion":N}`（运维操作：所有权变更） |
| POST | `/migration/export` | `{"partition","routingVersion"}` | 200 分区快照 JSON |
| POST | `/migration/import` | 快照 JSON | 200 `{"imported":true}`；409 快照版本过期 |

### 请求样例（curl）

```bash
# 新事件 / 重复 / 乱序重复
curl -X POST localhost:8080/events -d '{"eventId":"evt-001","eventTime":1700000000000,"routingVersion":1}'
# 推进水位使条目过期
curl -X POST localhost:8080/watermark -d '{"partition":3,"watermark":1700000000500,"routingVersion":1}'
# 迁移：升版本 → 导出 → 在另一节点导入
curl -X POST localhost:8080/migration/bump -d '{}'
curl -X POST localhost:8080/migration/export -d '{"partition":3,"routingVersion":2}' > snap.json
curl -X POST localhost:8081/migration/import -d @snap.json
```

## 验收覆盖（自动化测试）

`src/test/java/dedup/DedupServiceTest.java`（自带断言运行器，无 JUnit）：

1. **乱序重复** — 同 ID 以更旧时间戳乱序重投，窗口内仍判 duplicate。
2. **水位边界** — `watermark = eventTime + retention - 1` 时仍 duplicate；`== eventTime + retention` 时过期判 new。
3. **过期同 ID 视为新事件**。
4. **内存释放** — 1000 条条目在水位推进后全部淘汰（`totalEntryCount` 归零）；未到期条目不误删。
5. **错误路由版本拒绝** — 提交/导入均校验版本。
6. **迁移交接** — 导出→导入后，交接期间投递到新节点的重复事件仍判 duplicate；水位随快照转移，过期点一致；旧版本请求双侧拒绝。
7. **过期快照拒绝导入**。
8. **HTTP 端到端** — 真实启动两个 HttpServer 实例，走完整 export→import→duplicate 链路及 400/409 错误码。

## 实际运行记录（2026-09-24，OpenJDK 17.0.20.1，Ubuntu）

测试：

```
PASS  out-of-order duplicates
PASS  watermark boundary
PASS  expired id treated as new
PASS  memory released with watermark
PASS  stale routing version rejected
PASS  migration handoff
PASS  stale snapshot rejected
PASS  HTTP end-to-end
RESULT: 8 passed, 0 failed   (exit=0)
```

示例（`--partitions=4 --allowed-lateness-ms=0 --retention-ms=500`，实际输出）：

```
POST /events {"eventId":"evt-001","eventTime":1700000000000,"routingVersion":1}
  -> {"status":"new","partition":3,"watermark":1700000000000}
POST /events （同上）                       -> {"status":"duplicate",...}
POST /events （同 ID，eventTime 更早）      -> {"status":"duplicate",...}     # 乱序重复
POST /events routingVersion=99            -> HTTP 409 {"error":"stale_routing_version","expected":1,"actual":99}
POST /events 缺 eventTime                 -> HTTP 400 {"error":"missing or invalid 'eventTime'"}
POST /watermark {partition:3, watermark:1700000000500} -> {"evicted":1}      # 内存释放
POST /events evt-001                      -> {"status":"new",...}            # 过期后同 ID 视为新事件
POST /migration/bump                      -> {"routingVersion":2}
POST /migration/export {partition:3}      -> {"partition":3,"routingVersion":2,"watermark":1700000000500,"entries":{"evt-001":1700000000000},...}
POST /events routingVersion=1             -> HTTP 409                         # 旧版本被拒
（第二节点 :8081）
POST /migration/import @snap.json         -> {"imported":true,"routingVersion":2}
POST /events evt-001 routingVersion=2     -> {"status":"duplicate",...}      # 交接后重复仍被去重
（B 节点 bump 到 3 后）再次导入旧快照      -> HTTP 409                         # 过期快照被拒
```

## 已知限制 / 未完成项

- 状态仅在内存中，进程重启即丢失（快照可导出到文件/对端，但没有内建持久化与自动恢复）。
- 路由版本与迁移由运维/调用方驱动（`bump`/`export`/`import` 接口），服务本身不做成员管理与自动再均衡。
- 分区数固定，不支持在线扩缩容（扩缩容需按分区逐一迁移）。
- 单分区条目表为 HashMap，淘汰是 O(n) 扫描；超大规模单分区可改为按事件时间排序的堆优化。
- 线程模型为每分区锁 + 8 线程 HTTP 池，未做背压与限流。
