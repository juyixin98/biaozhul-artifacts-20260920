# 运行记录（RUN_LOG）

本文件如实记录本项目在本机的实际执行情况：环境、命令、真实输出，以及开发过程中
出现过的失败与修复。无“一次通过”的修饰——中途的失败也保留在文末。

- 记录时间：2026-09-24（CST）
- 环境：Linux 6.8.0-90-generic (x86_64)，`openjdk 21.0.12.1`（仅 JDK，无 Maven/Gradle，无网络依赖）
- 工作目录：`/home/admin/Downloads/biaozhul/opp123/a`

## 1. 编译

命令：

```bash
./build.sh
```

结果（退出码 0，无告警）：

```
Main sources compiled into build/classes
```

## 2. 自动化测试

命令：

```bash
./run-tests.sh
```

最终结果（退出码 0）：

```
== Compiling main ==
== Compiling tests ==
== Running tests ==

================================================
 Test summary: 651 passed, 0 failed
================================================
```

覆盖点（与验收要求对应）：

| 验收点 | 测试位置（Tests.java） | 结果 |
|---|---|---|
| 空文档、仅标点文档 | `testEmptyDocuments`、`testSyntheticCorpusPagination`（empty-not-ranked） | 通过 |
| 重复词（词频饱和、长度归一化） | `testRepeatedTermSaturation`（含同长度 tf=5/tf=10 饱和比 < 2× 对照） | 通过 |
| 同分结果按 docId 排序 | `testTieBreakerDocId`、`testSyntheticCorpusPagination`（tie-001<tie-002）、HTTP 端到端断言 | 通过 |
| BM25 数值正确性 | `testBm25HandComputed`（N=3 手算，误差容限 1e-12） | 通过 |
| 连续分页无漏无重 | `testPaginationNoGapsNoDupes`（页大小 1/2/7/13/100，多词与重复查询词） | 通过 |
| 更新不影响旧游标页面 | `testSnapshotIsolation`（upsert+delete 产生 v2/v3，旧游标仍读 v1，新旧可交错） | 通过 |
| 快照过期错误 | `testSnapshotExpiry` + HTTP 端到端（410 `SNAPSHOT_EXPIRED`，版本号核对） | 通过 |
| 损坏游标错误 | `testInvalidCursor` + HTTP 端到端（400 `INVALID_CURSOR`） | 通过 |

## 3. 实际启动服务并手工（curl / Python HTTP 客户端）验证

命令：

```bash
BM25_PORT=8080 java -cp build/classes com.bm25pager.Main
```

启动输出：

```
==========================================================
 BM25 stable-pagination service
 Listen port     : 8080
 Corpus documents: 69
 Current version : 1
 Retain snapshots: 5
 Health check    : GET  http://localhost:8080/health
 Search          : POST http://localhost:8080/search
==========================================================
```

### 3.1 同分排序（banana，pageSize=2）

第一页（见 `examples/out/01-search-banana-p1.json`）：`tie-003`（5.5956）排第一，
`tie-001`（5.327528165281776）第二；`nextCursor` 非空。

第二页（见 `examples/out/02-search-banana-p2.json`）：

```json
{
  "version": 1,
  "page": 1,
  "totalHits": 3,
  "hits": [ { "docId": "tie-002", "score": 5.327528165281776, ... } ],
  "nextCursor": null,
  "hasMore": false
}
```

`tie-001` 与 `tie-002` 分数逐位相同（5.327528165281776），按 docId 升序先后出现；
末页 `nextCursor=null`。

### 3.2 连续分页无漏无重（query=data，60 条命中）

用脚本从第一页起跟随 `nextCursor`（pageSize=7）：

```
data walk: 9 pages, 60 docs, no gap/dupe, pinned v1
```

另一次同脚本校验（页大小 7，逐页断言 `version==1`、页码连续）：

```
pages: 9
collected: 60 totalHits: 60
unique: 60
NO-GAP-NO-DUP: OK, version pinned to v1 throughout
```

并对 60 条完整排名校验了 `(-score, docId)` 键全局有序：

```
ORDERING: score-desc/docId-asc OK on 60 hits
```

### 3.3 更新不影响旧游标（快照隔离）

在 v1 上取到旧游标后，依次：

- `POST /documents/upsert`（`live-new`，data 重复 15 次）→ 自动发布 **v2**
- `DELETE /documents/common-0001` → 自动发布 **v3**

随后：

```
old cursor page2 version: 1
SNAPSHOT ISOLATION: old cursor stays on v1, new doc absent
fresh query version: 3 top: live-new
FRESH QUERY: sees newest snapshot, live-new ranks first, common-0001 gone
old cursor page3 version: 1
```

旧游标继续翻页仍在 v1（看不到新文档、被删文档仍在旧排名中）；
全新查询落在 v3（新文档排第一、被删文档消失）；两类游标可交错使用。
留档：`examples/out/06-upsert.json` … `09-fresh-query-v3.json`。

### 3.4 快照过期

`POST /admin/compact?keep=1` 驱逐 v1/v2 后，用 v1 旧游标继续翻页：

```json
HTTP 410
{
  "error": "SNAPSHOT_EXPIRED",
  "message": "snapshot version 1 has expired; current version is 3",
  "requestedVersion": 1,
  "currentVersion": 3
}
```

损坏游标：

```json
HTTP 400
{ "error": "INVALID_CURSOR", "message": "cursor is not valid base64" }
```

留档：`examples/out/10-compact.json`、`11-snapshot-expired.json`、`12-invalid-cursor.json`。

### 3.5 边界与错误请求

- 空查询 `{"query":""}` → 200，`totalHits:0`（见 `examples/out/04-empty-query.json`）；
- 未知词 `zzzznoterm` → 200 空结果（`examples/out/05-unknown-term.json`）；
- 重复词饱和：query=repeat 时 `repeat-001` 6.7150 高于 `repeat-002` 5.9518，
  但远小于 15 倍，体现饱和；
- 坏 JSON → 400 `BAD_REQUEST`；缺 `query` 字段 → 400；未知路由 → 404 `NOT_FOUND`；
- `GET /admin/status` 留档 `examples/out/13-status.json`（文档数、词项数、平均长度等）。

全部留档文件位于 `examples/out/`（16 个），均为真实请求/响应，文件名含 HTTP 状态行。

## 4. 开发过程中实际出现过的失败及处置（如实记录）

首版代码第一次跑测试时 **643 过 / 6 失败**，另有 1 个编译错误和 1 个挂起，均已修复：

1. **编译错误**：测试中一处多余括号 `get("docId")))` → 修正后通过编译。
2. **6 条断言失败，全部是测试预期写错（实现符合规格）**：
   - 分词：误把 `token10` 预期切成 `token/10`、误把 CJK 相邻英文 `index` 计入
     （实际测试串里根本没有该英文词）。按既定规则 `[A-Za-z0-9]+` 修正预期；
   - BM25：手算时把 Lucene 风格 IDF 的 `1 + x` 误算成直接算 `x`
     （`ln(1.6)` 误写成 `ln(2.4)`），改正手算基线，与实现和公式一致；
   - `avgDocLength`：测试语料里非空文档长度误记为 2（实际 3 个 token），改正；
   - pageSize：非正值按规格夹到下限 **1**，测试误预期为缺省值 10，改正。
3. **测试通过后 JVM 不退出（挂起）**：`ApiServer` 使用的固定 8 线程池是非守护线程，
   之前因有失败用例调用 `System.exit(1)` 才未暴露。改为在 `ApiServer.stop()` 中
   `executor.shutdownNow()`，现在测试进程正常退出。
4. **语义修正**：空查询串最初返回 400；复核后认为“字段存在但为空”应返回 200 空结果集，
   只有缺字段/类型错才是 400。修改 `ApiServer` 并新增对应 HTTP 断言。

修复后重新全量运行：**651 passed, 0 failed，退出码 0**，编译零告警。
当前未发现未通过项。

## 5. 复现步骤汇总

```bash
./build.sh                 # 编译（javac，JDK21）
./run-tests.sh             # 651 条断言（含真实 HTTP 端到端），退出码 0
./run.sh                   # 启动 :8080
bash examples/requests.sh  # 对运行中的服务执行全套 curl 样例
```
