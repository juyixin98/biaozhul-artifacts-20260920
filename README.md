# segindex — 分段倒排索引（段合并 / 删除标记 / 快照查询）

纯后端 Java 项目：本地文本检索库 + JSON HTTP 服务。不调用任何外部搜索服务或大模型，测试输入为程序生成的合成语料。

## 功能与设计

- **分段倒排索引**：写入先进入内存 buffer，`commit()` 时落盘为一个不可变段（`postings.json` 词项→倒排表，`docs.json` 文档原文）。
- **删除标记（tombstone）**：删除只向 manifest 追加 `(docId, maxGen)` 标记，使该 ID 所有 `generation ≤ maxGen` 的倒排项在查询与合并时失效；数据本身由后台合并物理清除。
- **文档 ID 重用（代次）**：同一 ID 每次写入代次 +1。更新 = 作废旧代次 + 写入新代次；删除后再重建会得到更高代次，不受旧 tombstone 影响。查询对每个 ID 只返回最高存活代次。
- **后台合并**：段数达到 `--merge-factor` 时，守护线程把全部存活段合并为一个（应用 tombstone、物理清除已删文档），随后原子切换 manifest 并删除旧段目录。
- **一致快照查询**：`IndexReader` 在打开时把当前 manifest 引用的段完整读入内存，之后的 commit / merge / 段文件删除对它不可见；要看新数据就新建 reader。
- **崩溃安全（只发布完整新段）**：
  - 段先写入 `_tmp_<seg>/`，fsync 后原子 rename 为 `<seg>/`；
  - manifest 先写 `manifest.json.tmp`，fsync 后原子 rename；
  - 崩溃最多留下未被 manifest 引用的孤儿段目录或 tmp 文件，`Index.open()` 时全部清理——已发布状态永远是最后一个完整 manifest。

### 磁盘布局

```
index-data/
  manifest.json        # 段列表、每 ID 最新代次、tombstone 列表（原子替换）
  seg_000001/
    postings.json      # term -> [{docId, generation, freq}]
    docs.json          # "docIdgeneration" -> 原文
  seg_000002/ ...
  _tmp_seg_000003/     # 写入中的段（崩溃残留，重启时清理）
```

## 构建与运行

```bash
mvn package          # 编译 + 跑测试 + 生成 target/segindex-1.0.0.jar（含依赖）
mvn test             # 只跑测试

java -jar target/segindex-1.0.0.jar --port 8080 --data ./index-data --merge-factor 4
```

参数：`--port`（默认 8080）、`--data`（索引目录，默认 `./index-data`）、`--merge-factor`（默认 4）。每个写请求同步 commit，返回成功即已持久化。

## JSON API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/docs` | 写入/更新文档，body `{"id":"doc-1","text":"..."}`，返回新代次 |
| DELETE | `/docs/{id}` | 删除文档（写 tombstone 并 commit） |
| GET | `/search?q=<term>&limit=100` | 单词项查询，返回命中 ID、代次、词频、原文 |
| GET | `/segments` | 段列表、tombstone 数、跟踪的文档 ID 数 |
| POST | `/merge` | 同步触发全量合并 |
| GET | `/health` | 健康检查 |

请求样例见 [examples/requests.sh](examples/requests.sh)（可直接执行）。

```bash
curl -s -X POST localhost:8080/docs -d '{"id":"doc-1","text":"the quick brown fox"}'
curl -s 'localhost:8080/search?q=quick'
curl -s -X DELETE localhost:8080/docs/doc-1
```

## 自动化测试（验收覆盖）

`src/test/java/com/example/segindex/`，共 12 个用例：

| 测试 | 覆盖的验收项 |
|------|--------------|
| `GroundTruthTest` | 1500 个随机交错操作（增/删/commit/merge/重启/查询），每次查询与内存模型全文扫描逐词对比 |
| `GenerationTest` | 同 ID 更新/删除/重建的代次语义；重启后代次不重置；未 commit 写入不可见 |
| `SnapshotTest` | reader 打开后发生 commit/删除/merge，旧 reader 结果不变 |
| `MergeTest` | 后台合并收敛段数、结果不变、tombstone 物理清除（检查段文件内容） |
| `CrashRecoveryTest` | 在段写入中、段 rename 后 manifest 提交前、合并 manifest 切换前三个点位注入模拟崩溃，重启后：状态一致、孤儿段被清理、查询与模型一致、索引可继续工作；撕裂的 manifest.tmp 被忽略 |
| `ServerTest` | HTTP 端到端：增删查、同 ID 更新、状态与合并接口、4xx 错误处理 |

## 实际运行记录（如实）

环境：OpenJDK 17.0.20.1，Maven 3.8.7，Linux 6.8（x86_64）。

- `mvn test`：**12/12 通过**（Tests run: 12, Failures: 0, Errors: 0）。
- `java -jar target/segindex-1.0.0.jar --port 18080 --data /tmp/segindex-demo --merge-factor 3`：手工 curl 全流程（增→查→同 ID 更新→查旧词为空→删→查→合并）结果全部符合预期；后台合并在段数达到 3 时自动触发（5 次 commit 后段数收敛为 2）。
- 对运行中的服务 `kill -9` 后重启：数据完整，`search?q=quick` 仍返回 `doc-1` 第 2 代，目录中无孤儿段。

### 开发中发现并已修复的问题

1. **同 ID 更新不作废旧代次**（正确性 bug，由 `GroundTruthTest` 对照全文扫描发现）：旧版本独有的词在更新后仍可搜到。修复：`addDocument` 在代次 >1 时自动为全部旧代次写 tombstone（更新 = 删除 + 新增）。
2. 段名分配竞态：合并与 commit 可能分配到相同段序号。修复：改为锁内统一分配器。
3. 测试自身问题两处：崩溃钩子首次触发误伤 setup 阶段的 commit（改为可武装触发）；`MergeTest` 用子串匹配检查段文件时 `"doc-2"` 命中 `"doc-20"`（ID 改为补零格式）。

### 已知限制

- 查询为单词项精确匹配（分词后小写），无短语/布尔/排序打分；结果按 docId 字典序返回。
- 段与 manifest 全量读入内存，面向中小规模语料；未做倒排表压缩。
- 非全量合并时 tombstone 保留在 manifest 中（可能作用于未合并的段），全量合并后清空。
