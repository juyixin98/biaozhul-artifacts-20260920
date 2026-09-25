# segment-merge-index — 分段倒排索引与后台合并

纯后端 Java 21 项目：本地文本检索库 + JSON HTTP 服务。不调用任何外部搜索服务或大模型；输入为自建合成语料。

## 功能

- **分段倒排索引**：RAM 缓冲 → 刷盘为不可变段（`seg_NNNNNN.seg`），词项 → 排序 posting 列表
- **删除标记**：删除/更新产生墓碑（tombstone），追加到带校验和的 `deletes.log`，合并时物理回收
- **后台合并**：定时器按合并因子挑选最小段合并；也可 `POST /merge` 全量合并
- **一致快照**：每次查询在单锁内捕获「manifest 版本 + 段集合 + 墓碑集合 + RAM 视图」的不可变快照；快照获取后，后续更新/删除/合并/刷盘均不影响其结果
- **文档 ID 重用（代次）**：同一 id 每次更新 gen+1；删除后可重新使用同 id，新代次自动递增；旧代次永不复活
- **崩溃只发布完整新段**：段先写 `*.seg.tmp` + fsync → 原子 rename → 原子替换 `manifest.json`（提交点）→ 目录 fsync；崩溃只会留下未提交的临时/孤儿文件，启动时清理

## 构建与运行

```bash
mvn package                      # 编译 + 全部测试 + 打包
java -jar target/segment-merge-index-1.0.0.jar \
     --dir /tmp/ivx --port 8080 --docs 200
```

主要参数：`--buffer N`（RAM 缓冲大小）、`--merge-factor N`、`--merge-interval-ms N`、`--no-background-merge`、`--crash-at POINT`（崩溃注入演示，见下）。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/documents` | `{"text":"..."}` 或 `{"id":N,"text":"..."}`，返回 `{"id","gen"}` |
| GET | `/documents/{id}` | 当前 live 版本（含 gen、所在段）；404 表示无 |
| DELETE | `/documents/{id}` | 删除当前版本；`{"deleted":true/false}` |
| GET | `/search?q=...` | 词项查询；`AND:a,b` / `OR:a,b` 布尔查询 |
| POST | `/search` | `{"q":"AND:segment,merge"}` |
| POST | `/flush` | 强制 RAM → 新段 |
| POST | `/merge` | 合并全部段 |
| GET | `/stats` | 段数、墓碑数、live 文档数等 |

请求样例见 [`examples/requests.http`](examples/requests.http) 与可执行脚本 [`examples/curl-examples.sh`](examples/curl-examples.sh)（`./examples/curl-examples.sh http://127.0.0.1:8080`）。

## 架构

```
data/
  manifest.json          提交点：段列表 + 代次计数器 + 版本号（原子替换）
  deletes.log            墓碑/代次推进日志，单条记录一次 fsync，尾部撕裂可修复
  segments/
    seg_000001.seg       不可变段：stored 字段 + 排序 posting + SHA-256 尾部
    seg_000002.seg.tmp   崩溃遗留的临时文件（启动时清理）
```

关键不变式：

1. **manifest 是唯一提交点**。段文件先完整落盘（tmp→rename），manifest 原子替换后才对查询可见；崩溃后磁盘上只存在「完整旧状态」或「完整新状态」。
2. **墓碑先于状态变更落盘**。删除/替换已发布版本时，`deletes.log` 先 fsync（单条记录同时含墓碑与代次推进，撕裂则整条回滚），内存状态随后才更新。
3. **代次单调**。`nextGen` 取 manifest、kill 日志、磁盘数据三者最大值恢复；同一 (id, gen) 永不复用，旧代次 posting 即使残留在段中也被快照过滤。
4. **合并不可见中间态**。合并结果先写 `merge_build_*` 临时文件，提交时在写锁内 rename 为正式段并替换 manifest；源段文件在 manifest 提交后才删除，崩溃遗留由启动清理回收。
5. **查询快照自洽**。`Snapshot` 一次性绑定段集合 + 墓碑集合 + 最新版本映射；并发合并换段、并发 flush 增段都不影响已获取快照。

## 自动化测试（22 个用例，全部通过）

| 测试类 | 覆盖点 |
|---|---|
| `SegmentFormatTest` | 段序列化往返、校验和/截断拒绝 |
| `DeleteLogTest` | 墓碑回放、撕裂尾部修复、kill 记录原子性 |
| `GenerationTest` | 同 ID 更新代次递增、删除后重用、跨重启代次、RAM 内替换 |
| `MergeTest` | 合并回收墓碑与旧代次、全删段合并、后台合并收敛 |
| `SnapshotIsolationTest` | 快照在更新/删除/合并后保持冻结 |
| `RandomInterleaveOracleTest` | **验收核心**：12 组随机历史 × ~300–700 步交错增删改/flush/merge，每 5 步与全文扫描对照 id+gen |
| `ConcurrencyStressTest` | 4 写线程 + 1 读线程 + 后台合并，最终每 id 恰一个 live 版本且代次正确 |
| `CrashRecoveryTest` | 6 个崩溃注入点逐一硬停后恢复；孤儿清理；kill 日志撕裂修复 |
| `HttpApiTest` | HTTP 全生命周期（增/改/删/查/flush/merge/stats/错误码） |
| `CorpusGeneratorTest` | 合成语料确定性 |

运行：`mvn test`。覆盖率（JaCoCo）：行 84.4%，指令 85.3%。

## 实测记录（真实命令与结果）

以下命令均在本机实际执行（Java 21.0.12.1，Maven 3.8.7，Linux）。

### 1. 测试与覆盖率

```bash
$ mvn -o test
...
Tests run: 22, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

JaCoCo 汇总：`INSTRUCTION 85.3% (4990/5852)`，`LINE 84.4% (944/1118)`，`BRANCH 73.6%`。

### 2. 服务启动 + 合成语料

```bash
$ java -jar target/segment-merge-index-1.0.0.jar --dir /tmp/ivx-demo --port 18080 --docs 60
seeded 60 synthetic documents
inverted-index service on http://127.0.0.1:18080

$ curl -s http://127.0.0.1:18080/stats
{"publishedSegments": 2, "segmentNames": ["seg_000010","seg_000011"],
 "bufferedDocs": 0, "tombstones": 0, "liveDocs": 60, ...}
```

60 篇文档被后台合并收敛为 2 段。`curl -s ".../search?q=espresso"` 返回 5 条命中。

### 3. 交错增删改 + 同 ID 更新（`examples/curl-examples.sh` 节选）

```bash
$ curl -s -X POST $B/documents -d '{"id":500,"text":"segment merge policy note"}'
{"id": 500, "gen": 1}
$ curl -s -X POST $B/documents -d '{"id":500,"text":"segment merge policy revised"}'
{"id": 500, "gen": 2}          # 同 ID 更新 → 代次 +1
$ curl -s "$B/search?q=segment"   # 命中 id=500 gen=2（gen=1 不再出现）
$ curl -s -X DELETE $B/documents/500
{"id": 500, "deleted": true}
$ curl -s -i $B/documents/500 | head -1
HTTP/1.1 404
```

### 4. 合并中断（merge.rename 点硬停）→ 恢复

```bash
# 准备：24 文档、buffer=3 制造 9 段；更新 id=4（gen 2）、删除 id=10
$ java -jar target/segment-merge-index-1.0.0.jar --dir /tmp/ivx-crash3 \
    --port 18087 --docs 0 --merge-factor 3 --merge-interval-ms 200 \
    --crash-at merge.rename
CRASH-INJECTION: hard halt at merge.rename     # JVM 硬停（等价 kill -9）

# 崩溃现场：磁盘 10 个段文件，manifest 仍指向 9 段 → seg_000010.seg 是孤儿
$ python3 -c "import json;d=json.load(open('/tmp/ivx-crash3/manifest.json'));print(len(d['segments']))"
9
$ ls /tmp/ivx-crash3/segments | wc -l
10

# 重启恢复
$ java -jar target/segment-merge-index-1.0.0.jar --dir /tmp/ivx-crash3 --port 18088 --docs 0 --no-background-merge
$ ls /tmp/ivx-crash3/segments | wc -l
9                              # 孤儿段已清理
$ curl -s $B/documents/4
{"text": "UPDATED lunar rocket payload", "id": 4, "gen": 2}   # 更新版本存活
$ curl -s -o /dev/null -w "%{http_code}" $B/documents/10
404                            # 删除保持
$ curl -s "$B/search?q=portfolio"   # id=4 旧代次文本未泄漏（其余文档命中 3 条）
$ curl -s -X POST $B/merge
{"mergedSegments": 9}
$ curl -s $B/stats
{"publishedSegments": 1, "liveDocs": 23, "tombstones": 2}
# 再次干净重启：1 段、23 文档、id=4 gen=2 依旧 —— 合并结果持久
```

### 5. 未提交 RAM 数据丢失但索引一致（flush.rename 点）

JUnit `CrashRecoveryTest` 覆盖全部 6 个注入点（`flush.tmp` / `flush.rename` / `flush.manifest` / `merge.tmp` / `merge.rename` / `merge.manifest`）：崩溃实例直接弃用（不执行 close，模拟真实硬停），重开后断言——孤儿文件清零、已提交数据完整、未提交 RAM 批次整体不可见、代次计数器不回退。

## 已知限制

- 布尔查询仅支持平铺 `AND:`/`OR:`（无括号嵌套、无短语/位置查询）。
- 合并构建在写锁外进行，但提交与源段删除串行；大索引下合并吞吐有限。
- `deletes.log` 只追加不压缩（合并不回收日志本身），长期运行需外部轮转。
- 崩溃会丢失未 flush 的 RAM 批次（设计使然：索引保持一致，客户端需重放未确认写入）。
