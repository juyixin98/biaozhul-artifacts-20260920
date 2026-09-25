# 验证记录（Verification Log）

环境：Ubuntu，OpenJDK `21.0.12.1`，无 Maven/Gradle/外部依赖，全部用 `javac`/`java` 运行。

## 执行的命令

```bash
scripts/build.sh          # 编译 main + test
scripts/test.sh           # 38 个自动化测试
java -cp target/classes streamagg.Main 18091   # 启动服务
scripts/demo.sh 18091     # curl 端到端验收（16 个场景）
```

原始输出：

- `docs/build-log.txt` — 干净构建（`BUILD OK`，退出码 0）
- `docs/test-log.txt` — `# tests 38 / # pass 38 / # fail 0`，退出码 0
- `docs/run-log.txt` — 服务真实运行的 16 个场景完整请求/响应，退出码 0

## 结果摘要（最终状态）

- 自动化测试：**38/38 通过**，无未通过项。
- HTTP 验收：先撤销后新增（带版本号与不带版本号两种）、乱序 `v3` 更正缓存与级联释放、
  多次更正（改值/改键）、重复 `opId` 幂等、撤销终止链后的更正冲突拒绝、
  what-if 原始流重放、当前账本独立重算 —— 全部与预期一致；
  4 处 `matchesLedgerRecomputation: true`，`pendingOps: 0`，无计数负漂移。

## 开发过程中发现并修复的问题（如实记录）

第一次跑通全量测试时为 **28 通过 / 9 失败**，逐个定位后全部修复，最终 38/38：

1. 测试断言对 `Integer`/`Long`/`BigDecimal` 装箱比较判不等（6 处）—— 断言工具改为数字感知比较。
2. 手写 JSON 写入器不支持 Map 内嵌套已解析的 `JsonValue` —— 写入前统一解包。
3. 精确参考实现对「retract 一个不存在的事件」先抛 NPE 而非负漂移错误 —— 增加显式守卫，
   抛出带事件/键信息的 `negative count drift detected`。
4. `/v1/events` 的 pending 视图只遍历账本中出现过的事件 ID，纯挂起事件不可见 ——
   引擎新增 `eventIdsWithPending()` 后修复。
5. `/v1/replay` 空 body 分支错误地把「已解析账本」当作版本化原始操作再喂引擎，导致依赖被
   错误缓存 —— 改为直接调用精确参考实现 `recomputeFromLedger` 独立重算并逐键比对。
6. **真实语义缺陷**：事件在 `v2 RETRACT` 终止版本链后，再来 `v3 CORRECT` 会得到
   `BUFFERED` 且永远挂死（0 pending 但不解析）；若 `v3` 在撤销前就已缓存，链终止后也永久
   残留 —— 修复为：终止后高版本操作返回 `CONFLICT`，终止时把已缓存高版本全部清为冲突并
   记 warning（新增专门单测覆盖两条路径）。

## 已知边界

- 定位为小数据/单实例库：状态保存在内存，未做持久化与分布式扩展（需求明确无需外部消息系统）。
- HTTP 服务仅监听 `127.0.0.1`，无鉴权，面向本地/可信网络使用。
