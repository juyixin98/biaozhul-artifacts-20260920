# RUNLOG —— 实际运行记录

本文件如实记录在本机实际执行的命令、结果，以及开发过程中出现过的失败、原因与处置。所有时间为 UTC。

- 机器：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04 容器）
- JDK：`openjdk 21.0.12.1`（`java` / `javac` 同版本）
- 构建工具：**无 Maven / 无 Gradle**，纯 `javac`；除 JDK 外无第三方依赖
- 网络：构建与测试**不联网**（仅最初探测过 Maven Central 可达性，最终方案不使用）

---

## 1. 环境探测

```text
$ java -version ; javac -version ; which mvn gradle
openjdk version "21.0.12.1"  ...
javac 21.0.12.1
(无 mvn / gradle)
```

据此选定**零依赖**方案：JDK 内置 `com.sun.net.httpserver.HttpServer` 提供 JSON 服务；自带最小 JSON 解析器与最小测试框架，保证离线可复现。

---

## 2. 干净构建

命令：

```bash
rm -rf build && ./build.sh
```

结果（`build/runlog-build.txt`）：

```text
[build] javac main sources
[build] OK -> build/classes
build exit=0
```

---

## 3. 自动化测试

命令：

```bash
./run_tests.sh
```

最终结果（`build/runlog-tests.txt`）：

```text
Tests: 54 passed, 0 failed, 0 skipped
exit=0
```

测试清单（按分组，全部 PASS）：

- 去重单元 11 个：首次/重复、**同 id 不同载荷**、**同 id 不同事件时间 skew**、水位线释放边界、释放后 `unverified`、品牌新 id 的旧时间也标 unverified、skew 延长保留期、容量淘汰、淘汰后重复 unverified、快照恢复
- 墓碑单元 7 个：事件时间压制、删除后重建通过、未知 key、TTL 过期→uncertain、边界保留、容量淘汰、快照恢复
- 窗口单元 9 个：分桶（含负时间 floor）、触发时刻、迟到丢弃、墓碑窗口、多 key/多窗口顺序、无水位线不触发、快照恢复
- 水位线单元 6 个、时间/调度单元 3 个、JSON 单元 5 个
- **验收 9 个**：AC-0 承诺无重复、AC-1~AC-6（含同 id 不同载荷、时钟回退、重启恢复、极迟重复、墓碑、载荷哈希 key 顺序无关）
- 差异测试 2 个（DIFF-1 去重逐事件比对；DIFF-2 窗口逐窗口比对）
- HTTP 黑盒 3 个（含**落盘快照 + 新进程重启恢复**、时钟回退）

---

## 4. 端到端演示（真实启动 HTTP 服务 + curl）

命令：

```bash
PORT=18099 ./demo.sh
```

结果（`build/runlog-demo.txt`，738 行，exit=0）关键事实摘录：

- 同 id 重投：第二条 `evt-1001` → `"duplicate": true, "emitted": false`；首次两条 `acceptedOut=2, duplicatesIn=1`
- 同 id 不同载荷：`payloadMismatch=true`；同 id 不同事件时间：`eventTimeSkew=true`
- **时钟回退**：`/tick` 从 1000 推进到 -4000（回退 5000ms）后重放 `evt-4001`，仍为 `"duplicate": true, "emitted": false`
- 墓碑：DELETE@2500 之后，乱序到达的 UPSERT@2300 `suppressedByTombstone=true, emitted=false`；UPSERT@2800（删除后重建）`emitted=true`
- 排水窗口：`order:7` 为 `upsertCount=2, deleteCount=1`（被压制的乱序 UPSERT 未计入）
- **极迟重复**：水位线推进到 120000（远超保留期）后重放旧 `evt-4001`：
  `"late": true, "dedupUnverified": true, "windowLateDropped": true, "emitted": false`
- 最终指标：`eventsIn=15, acceptedOut=9, duplicatesIn=4, payloadMismatches=1,
  eventTimeSkews=1, unverifiedDuplicates=1, tombstoneSuppressed=1, windowLateDropped=1,
  capacityEvictions=0, watermarkEvictions=10`

---

## 5. 独立进程重启恢复（CLI 快照文件路径）

除 JUnit 的 HTTP 重启用例之外，另用 shell 启动**两个独立 JVM 进程**验证 `--snapshot-file`：

1. 进程一：摄入 `restart-1@1000`（emitted=true），水位线推进到 5000，`/snapshot` 落盘后 `kill`；
2. 进程二：用同一快照文件启动（启动日志含 `snapshotFile=...`），重放同一事件：

```text
after restart -> duplicate: True emitted: False
watermark: 5000
```

即去重状态与水位线跨进程延续。完整输出见 `build/restart-proc1.log` / `build/restart-proc2.log`。

---

## 6. 开发过程中出现过的失败与修复（如实记录）

首版测试**并非一次通过**。第一次完整运行结果为 **45 passed / 9 failed**。定位后分为三类：

1. **测试期望与事件时间语义写反（实现正确，改测试）**
   - 墓碑方向：初版部分测试按"到达顺序"期望"删除后到达的 UPSERT 一律压制"，而实现按**事件时间**（删除时间 ≥ upsert 时间才压制，删除后重建应通过）。统一为事件时间语义并在 README 明确说明，修正 3 处墓碑单测与 AC-5、AC-3 的期望。
   - AC-3 自身矛盾：在同一 key 上既发"删除后重建"的 upsert 又断言该窗口是纯 delete 窗口。删除该段（重建语义已由 AC-5 覆盖）。
2. **发现并修复了一个真实的语义缝隙（改主代码）**
   - 当去重保留期 < 窗口寿命（窗口大小+迟到宽限）时，存在"去重状态已释放、但窗口尚未关闭"的区间，极迟重复可能被再次折入窗口而重复计数。
   - 修复：在 `StreamPipeline` 构造时强制不变式
     `retentionMillis >= windowSizeMillis + windowLatenessMillis`（墓碑 TTL 同理）。
     于是任何**无法验证**的旧重复，其窗口必然已触发 → 只进迟到侧输出，**窗口输出永不重复计数**。
3. **差异测试数据生成器不真实 + JSON 解析过松（改测试/改解析器）**
   - DIFF-1/DIFF-2 早期给"重投"的同一 id 随机生成了**新的事件时间**（skew 重复），而无界参考实现永不去重，二者在"已过期 skew 重复"上必然分歧。改为真实重投语义——**重投携带相同事件时间**（skew 场景由单元测试单独覆盖），并将保留期设为覆盖窗口寿命，差异随即清零。
   - 自带 JSON 解析器最初接受 `01` 这种前导零数字；已按 JSON 规范拒绝前导零与缺少小数位的 `1.`。

修复迭代后：**54 / 54 通过**。

---

## 7. 当前未通过项 / 已知限制

- 截至最后一次运行，**无失败用例**（54/54，构建与演示 exit=0）。
- 已知的、**有意为之并已显式暴露**的非保证项（不是缺陷）：
  - 水位线越过承诺范围后的旧重复**不保证去重**，以 `dedupUnverified=true` 上报；
  - 墓碑 TTL 过期后的很旧 upsert 以 `tombstoneUncertain=true` 上报；
  - 容量打满触发淘汰时以 `capacityEvicted=true` / `capacityEvictions` 指标暴露，代表承诺范围被容量上界收窄；
  - 已触发窗口的迟到事件不折入结果，进入迟到侧输出（`windowLateDropped`）。
- 未做前端、未接入外部消息系统（按需求范围）。
