# 动态合并会话窗口（Dynamic Merging Session Window）

纯后端服务：把同一按键（`key`）的事件按相邻间隔聚合成会话；**迟到事件可以桥接两个已有会话**；
每次结果变化以「撤回（RETRACT）+ 新版本（UPSERT）」的变更日志（changelog）输出；
基于水位（watermark）定义迟到拒收规则；事件落盘，重启后重放日志，会话 ID 与聚合结果完全稳定。

仅使用 **JDK 17 标准库**（`com.sun.net.httpserver.HttpServer` 等），**没有任何第三方依赖、没有 Maven/Gradle**。

---

## 1. 语义定义

| 概念 | 定义 |
|---|---|
| 会话（session） | 同一 `key` 下，相邻事件时间差 `<= gap` 即属于同一会话；会话只合并不分裂 |
| 桥接（bridge） | 迟到事件落在两个已有会话之间，且距二者都在 `gap` 内，则把两个会话合并为一个 |
| 稳定会话 ID | 每个 key 内单调递增的本地序号（`key#1`、`key#2`…）；合并后保留**较小** ID，因此重放后 ID 不变 |
| 版本（version） | 会话每次更新（扩展/合并）加 1；撤回记录携带被撤回会话的最后版本号 |
| 变更日志 | `UPSERT`（放入新版本快照）/ `RETRACT`（删除该会话）；带全局连续 `seq` 与原子修订号 `revision` |
| 水位 W | `W = max(手动水位, 最大事件时间 - allowedLateness)` |
| 拒收规则 | 事件满足 `ts < W`（在水位到达**之后**到达）则拒收：不计数、不落盘、不产生变更；`ts == W` 仍接收（边界取等，与 `gap` 的 `<=` 约定一致） |
| 幂等 | 相同 `clientId` 只处理一次，重复提交返回 `DUPLICATE`，绝不重复计数 |

**修订原子性**：同一次提交产生的多条变更共享 `revision`（桥接时先 RETRACT 后 UPSERT）。
消费者应按 revision 成组折叠，任何时刻都不会看到中间态；即便朴素地按 seq 逐条折叠，
计数也只会短暂「少算」（被撤回的版本消失、新版本尚未放入），**绝不会多算**。

---

## 2. 目录结构

```
src/main/java/com/example/sessionwindow/
  Main.java           入口（参数解析）
  ApiServer.java      JDK HttpServer，HTTP 接口
  SessionEngine.java  核心：聚合、桥接、changelog、水位、拒收、恢复
  EventLog.java       追加式 JSONL 事件日志（append + fsync，启动重放）
  Session/Event/Change.java  领域模型
  Json.java           手写极简 JSON 解析/序列化（零依赖）
src/test/java/.../Tests.java  零依赖测试（含真实 HTTP 端到端用例）
scripts/build.sh / test.sh / run.sh
examples/acceptance-demo.sh   验收场景 curl 全流程（含重启恢复）
DEPENDENCIES.lock   锁定的工具链与「零三方库」声明
run/                运行数据与实测输出记录（test-output.txt、acceptance-demo-output.txt）
```

---

## 3. 依赖与启动命令

需要 **JDK 17+**（用了 records、switch 模式匹配）。无第三方依赖。

```bash
# 如机器上没有 java，可设置 JAVA_HOME（脚本均支持该环境变量）
export JAVA_HOME=/path/to/jdk-17

./scripts/build.sh          # javac 编译到 out/
./scripts/test.sh           # 运行自动化测试（13 项）
./scripts/run.sh 8080       # 启动服务（默认端口 8080）

# 等价的手工命令：
java -cp out/classes com.example.sessionwindow.Main \
     --port 8080 --gap 10 --allowed-lateness 10 --data-dir run/data
```

启动参数（均有默认值）：`--port 8080`、`--gap 10`、`--allowed-lateness 10`、`--data-dir run/data`。
时间戳 `ts` 单位抽象（验收用整数时间；实际可按毫秒使用），gap 与其同单位。

---

## 4. HTTP 接口与请求样例

Content-Type 均为 `application/json`。

### 4.1 提交单个事件 `POST /events`

```bash
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"key":"userA","ts":0,"clientId":"e-0"}'
```

响应（`status`：`ACCEPTED` / `REJECTED` / `DUPLICATE`，`changes` 为本次变更）：

```json
{"status":"ACCEPTED","revision":1,"watermark":-10,
 "changes":[{"seq":1,"revision":1,"kind":"UPSERT","sessionId":"userA#1",
             "key":"userA","version":1,"startTs":0,"endTs":0,"count":1,"watermark":-10}]}
```

字段：`key`（必填）、`ts`（必填数字；也接受 `timestamp` 别名）、`clientId`（选填，幂等键）、`payload`（选填）。

### 4.2 批量提交 `POST /events/batch`

```bash
curl -s -X POST localhost:8080/events/batch -H 'Content-Type: application/json' \
  -d '{"events":[{"key":"userA","ts":20,"clientId":"e-20"}]}'
```

### 4.3 水位 `GET/POST /watermark`

```bash
curl -s localhost:8080/watermark
curl -s -X POST localhost:8080/watermark -H 'Content-Type: application/json' -d '{"watermark":20}'
```

手动水位只增不减；不超过当前值的推进是空操作。水位推进也会落盘，重启后继续生效。

### 4.4 查询物化会话 `GET /sessions[?key=userA]`

```bash
curl -s 'localhost:8080/sessions?key=userA'
```

### 4.5 变更日志 `GET /output[?afterSeq=N]`

```bash
curl -s localhost:8080/output                 # 全量
curl -s 'localhost:8080/output?afterSeq=2'    # 增量拉取（轮询/下游同步用）
```

### 4.6 统计 / 健康检查 / 重置

```bash
curl -s localhost:8080/stats
curl -s localhost:8080/health
curl -s -X POST localhost:8080/reset -d '{}'
```

非法 JSON 返回 HTTP 400，方法不允许返回 405。

---

## 5. 验收场景（时间 0、20、迟到 10，gap=10）

一条命令复现（自动起停服务、真实 HTTP、含重启恢复）：

```bash
./examples/acceptance-demo.sh 18080
```

逐步过程（实测输出见 `run/acceptance-demo-output.txt`）：

1. 事件 `ts=0` → 会话 `userA#1`（count 1）。
2. 事件 `ts=20`（与 0 相距 20 > 10）→ 新会话 `userA#2`。此时两个会话。
3. **迟到事件 `ts=10`**（距 0、距 20 都恰好等于 gap=10；自动水位为 20-10=10，`ts == W` 不拒收）
   → 两个会话被桥接合并，同 revision=3 内输出：
   - `RETRACT userA#2`（撤回旧版本）
   - `UPSERT userA#1 version=3, startTs=0, endTs=20, count=3`（保留较小 ID 的新版本）
4. 折叠 `/output`：存活会话只有 `userA#1`，计数为 3，**全过程计数峰值不超过已接受事件数 3**（撤回不重复计数）。
5. 手动水位到 20：`ts=19` 返回 `REJECTED`（`19 < 20`），不计数；`ts=25` 正常接受为 version=4, count=4；
   重复 `clientId=e-10` 返回 `DUPLICATE`。
6. **杀掉进程并重启**，重放 `run/demo-data/event-log.jsonl`：
   - 会话仍为 `userA#1`，version=4，范围 0..25，count=4（ID 与最终聚合稳定）；
   - 水位恢复为 20；changelog 的 seq/revision/version 与重启前逐条一致。

---

## 6. 持久化与恢复

- 每个**被接受**的事件、每次有效的手动水位推进，在响应前以一行 JSON 追加到
  `<data-dir>/event-log.jsonl`，并 `flush + fsync`。
- 拒收 / 重复不落盘（它们是瞬时判断，无法也不应影响重放）。
- 启动时顺序重放日志：聚合、桥接、changelog 的 `seq`/`revision`/`version` 与会话 ID
  都按完全相同的次序重建（有测试逐字段比对）；若日志损坏则拒绝启动并报错，避免静默分歧。
- 修订号只随「落盘决策」增长，所以恢复后编号不漂移。

---

## 7. 自动化测试

`./scripts/test.sh`（零第三方测试框架；`Tests.java` 自带断言/统计，失败时退出码非 0）。
13 个用例：

1. 新会话 version 从 1 开始
2. 间隔恰好等于 gap 链式相连（0,10,20 一个会话）
3. 间隔 gap+1 拆成两个会话
4. **验收：迟到 ts=10 桥接 0 与 20**（RETRACT k#2 + UPSERT k#1，count 3，ID 保留较小值）
5. **changelog 折叠不重复计数**（逐条峰值 ≤ 接受数；按 revision 原子折叠恒等于接受数）
6. 自动水位边界：ts 恰等于水位仍可桥接
7. 手动水位后过早事件拒收且不计数
8. clientId 幂等（DUPLICATE）
9. **重启恢复**：会话/changelog/ID/水位逐字段一致
10. 重启后已记录的水位继续拒收
11. 多 key、多次桥接下的 ID 确定性
12. **真实 HTTP 端到端**：事件/批量/output/sessions/拒收/重复/400/reset
13. 不同 key 独立聚合

---

## 8. 实测结果（本机如实记录）

- 环境：Ubuntu 24.04.4 LTS，x86_64；OpenJDK 17.0.2（用户目录解压安装，机器无 sudo、apt 装不了系统包）。
- `./scripts/test.sh`：**13/13 全部通过，退出码 0**，转录见 `run/test-output.txt`。
- `./examples/acceptance-demo.sh`：**全流程通过，退出码 0**，转录见 `run/acceptance-demo-output.txt`；
  重启后 `userA#1 version=4 0..25 count=4`、水位 20，与预期一致。

---

## 9. 未完成项 / 已知边界（如实说明）

- **单进程内嵌存储**：事件日志是本地单个 JSONL 文件，没有集群复制/多实例共享；
  多实例部署需外挂存储（本次按「纯后端、零依赖」要求未引入）。
- **全量重放启动**：没有快照（snapshot），恢复时间随日志线性增长；长日志场景需加分段快照与 GC。
- **时间戳为整数 long**：不支持小数/带时区时间，单位由调用方约定。
- **水位是全局的**（不按 key 区分）；按 key 独立水位未实现。
- `/reset` 会删除日志且**无鉴权**，仅适合本地/受信网络演示，不要直接暴露公网。
- 无背压/分页：`/output` 全量返回全部变更，超长日志下应由调用方用 `afterSeq` 增量拉取。
- 线程模型为固定 8 线程 + 引擎内置 synchronized，吞吐面向演示与中小流量，未做压测。
