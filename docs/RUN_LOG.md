# 实际运行记录（RUN LOG）

- 机器：Linux 6.8.0-90-generic，x86_64
- 日期：2026-09-25
- JDK：`openjdk 21.0.12.1`（Temurin 打包，Ubuntu 24.04）；默认时区 Asia/Shanghai
- 构建：Apache Maven（依赖取自本机 `~/.m2` 缓存：Jackson 2.17.2、JUnit 5.10.2；网络可访问 Maven Central）

> 本文件如实记录命令与结果，包括开发过程中出现的失败项及修复。

## 1. 环境确认

```
$ java -version
openjdk version "21.0.12.1" 2026-08-18
$ javac -version
javac 21.0.12.1
```

## 2. 开发过程中出现并已修复的失败（如实记录）

| # | 命令 | 失败现象 | 原因与修复 |
|---|---|---|---|
| 1 | `mvn -q test` | `Source option 5 is no longer supported. Use 8 or later.` | 默认绑定的 maven-compiler-plugin 3.1 过旧，未读取 `maven.compiler.release`。在 pom 中显式锁定 compiler 插件 3.13.0 并配置 `<release>21</release>`。 |
| 2 | `mvn -q test` | `CommitLogEntry.java: cannot find symbol class List` | 漏写 `import java.util.List;`，补上后通过。 |
| 3 | `mvn -q test` | 测试编译失败：`cannot find symbol D2026_02_01 / D2026_03_10 / D2025_05_01` | 测试类缺少 3 个日期常量，补齐。 |
| 4 | `mvn -q test` | `rejectsInvertedInterval` 失败：期望 `BitemporalException`，实际抛 `IllegalArgumentException`（Tests run: 39, Failures: 1） | 区间构造器抛的是 `IllegalArgumentException`。在 `BitemporalStore.validateRequests` 中捕获并转换为统一的 `BitemporalException`，使协议层错误信封一致。修复后 39/39 通过。 |

此外开发中自查改掉两处测试文件里的无意义断言残行（不影响行为）。

## 3. 干净构建 + 全量自动化测试（最终结果）

```
$ mvn clean verify
...
Tests run: 45, Failures: 0, Errors: 0, Skipped: 0
[INFO] All coverage checks have been met.
[INFO] BUILD SUCCESS
```

45 个测试分布：IntervalTest 6、SubtractAllTest 6、BitemporalStoreTest 20、RequestServiceTest 13。

JaCoCo 覆盖率（`target/site/jacoco/index.html`，CSV 汇总）：

```
RequestService     lines 128/137   instr 94.0%
RecordJson         lines  12/12    instr 100%
TimeZoneInfo       lines   8/11    instr 89.7%
SeedData           lines  10/10    instr 100%
Interval           lines  22/23    instr 94.6%
BitemporalStore    lines 136/142   instr 94.9%
其余模型/数据类      100%
Main（CLI 胶水层）   0/33（排除在 80% 门槛外，由样例手动运行覆盖）
TOTAL              lines 333/385 = 86.5%，指令 90.0%
```

## 4. 打包

```
$ mvn package -DskipTests
target/bitemporal-records-1.0.0.jar            # fat jar（含 Jackson，约 2.4 MB，Main-Class 已配置）
target/original-bitemporal-records-1.0.0.jar   # 瘦 jar
```

## 5. 8 个样例逐一实跑

```
$ for f in samples/*.json; do
    java -jar target/bitemporal-records-1.0.0.jar "$f" \
      > "docs/run-output/$(basename $f .json).out.txt" 2>&1;
    echo "$(basename $f) exit=$?";
  done
01-info.json                          exit=0
02-asof-baseline.json                 exit=0
03-commit-correction.json             exit=0
04-insert-overlap-rejected.json       exit=1   ← 业务拒绝，错误信封，非零退出码（符合预期）
05-multi-change-tx.json               exit=0
06-scenario-retroactive.json          exit=0
07-asof-all.json                      exit=0
08-snapshot.json                      exit=0
```

完整逐字输出保存在 [`run-output/`](run-output/)。关键结果摘录：

### info：TZDB 版本

```json
"timezone": { "tzdbVersion": "2026b", "javaVersion": "21.0.12.1", "defaultZone": "Asia/Shanghai" }
```

### 追溯修订（03）写入的 3 行（物理行总数 5 → 8）

- rowId 6：Dev 残余 `valid [2025-01-01,2025-03-01)`，`recorded [2026-02-15,+inf)`
- rowId 7：TechLead 残余 `valid [2025-09-01,+inf)`，`recorded [2026-02-15,+inf)`
- rowId 8：新事实 Platform/SRE `valid [2025-03-01,2025-09-01)`，`recorded [2026-02-15,+inf)`
- 旧 rowId 1/2 被关闭为 `recorded [2026-01-01,2026-02-15)` 并保留（见 06 scenario 第 3 步命中旧行的输出）

### 重叠拒绝（04）

```json
{ "ok": false,
  "error": { "message": "INSERT rejected: overlapping current record (rowId=1) for entity E001 over valid interval [2025-01-01, 2025-07-01); new interval [2025-06-01, 2025-08-01) overlaps it (use CORRECTION to restate history)" } }
```
退出码 `1`（单独复测：`echo $?` = 1）。

### 同事务多变更（05）

一个事务（recordedFrom 均为 2026-03-01）写入 4 行：E003 Junior 插入 1 行；E002 Rep 拆成
`[2025-01-01,2025-02-01)`、`[2025-04-01,2025-10-01)` 两段续存 + Marketing/Analyst 新事实 1 行。

### scenario（06）三个观察时刻对照

| 步骤 | businessDate | observationDate | 结果 rowId / dept / role |
|---|---|---|---|
| 修订前查询 | 2025-05-01 | 2026-01-15 | 1 / Engineering / Dev |
| 修订后用旧观察时刻重查 | 2025-05-01 | 2026-01-15 | 1 / Engineering / Dev（历史不可变） |
| 修订后查询 | 2025-05-01 | 2026-06-01 | 8 / Platform / SRE |
| history @2026-06-01 | — | — | 三段相接：Dev `[01-01,03-01)` → SRE `[03-01,09-01)` → TechLead `[09-01,+∞)` |

### stdin 管道

```
$ echo '{"op":"asOf","entityId":"E003","businessDate":"2025-05-01","observationDate":"2026-01-15"}' \
    | java -jar target/bitemporal-records-1.0.0.jar
→ {"ok":true, ... "record":{"rowId":5,"entityId":"E003","department":"Finance","role":"Analyst", ...}}
```

## 6. 未通过项 / 遗留说明

- 最终状态：**无未通过测试**，`mvn clean verify` 全绿，覆盖率门槛达标。
- 已知范围限制（非缺陷）：
  - CLI 入口 `Main` 未计入行覆盖率门槛（薄胶水层），改由上述 8 个样例 + stdin 管道实跑覆盖；
  - 进程无状态、每次重新装载固定种子；跨提交链路通过 `scenario` 操作获得；
  - 时间粒度为日期，TZDB 版本只记录上报（实测 2026b），不参与计算。
