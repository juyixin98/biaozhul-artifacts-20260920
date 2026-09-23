# 运行记录（RUNLOG）

本文件如实记录实际执行的命令与结果。日期：2026-09-23。

## 环境

```text
OS:     Linux 6.8.0-90-generic (Ubuntu 24.04), x86_64, 16 核
JDK:    openjdk 21.0.12.1（javac 21.0.12.1）
依赖:   无第三方库；未使用 Maven/Gradle；HTTP 用 JDK 内置 com.sun.net.httpserver
```

## 一、最终干净构建与测试（通过）

命令：

```bash
rm -rf target out
./test.sh
```

实际结果（退出码 0）：

```text
编译完成: target/classes
[RUN ] NullBitmap        [ OK ]
[RUN ] SelectionVector   [ OK ]
[RUN ] ColumnTable       [ OK ]
[RUN ] JsonRoundTrip     [ OK ]
[RUN ] Tri               [ OK ]
[RUN ] Filter            [ OK ]
[RUN ] BatchBoundary     [ OK ]
[RUN ] AllNullColumn     [ OK ]
[RUN ] DuplicateSelection[ OK ]
[RUN ] ProjectAggregate  [ OK ]
[RUN ] InvalidSelection  [ OK ]
[RUN ] RandomDifferential[ OK ]
[RUN ] Server            [ OK ]
================================================
通过断言: 191，失败: 0，耗时: 858 ms
全部通过。
```

批边界断言对固定 13 行数据遍历 batchSize = 1..15，共 15 组，每组校验结果集与
`filterBatches == ceil(13/bs)`；随机差分 400 轮（普通表达式 + 随机重复 selection）
+ 100 轮（顶层 union），种子 20260923 固定可复现。

## 二、示例查询实际输出

### 1. `./run.sh examples/request_filter.json`（退出码 0）

```text
selectedRows = [1, 2, 4, 5]
投影列 id  = [20, 30, 50, 60]
投影列 amt = [null, 300, null, 600]
聚合 = {count(*)=4, amt_sum=900}
vector filterBatches/downstream = 2 2
enginesAgree = True
```

### 2. `./run.sh examples/request_union_duplicates.json`（退出码 0）

两个分支（id<=30 命中 0,1,2；status='PAID' 命中 1,4）多重集合并：

```text
selectedRows = [0, 1, 1, 2, 4]
投影 id = [10, 20, 20, 30, 50]
聚合 = {count(*)=5, sum(id)=130}
```

行 1 因被两个分支同时命中而出现两次；count 与 sum 均按多重集累计。

### 3. `./run.sh examples/request_selection_sparse.json`（退出码 0）

显式 selection `[7,2,2,4,0]`（排序、保留重复），再过滤 id>1：

```text
selectedRows = [2, 2, 4, 7]
投影 = {id: [3, 3, 5, 8]}
聚合 = {count(*)=4, sum(id)=19}
vector 批次 = {filterBatches: 2, downstreamBatches: 4}
```

下游对长度 5 的选择向量按 batchSize=3 切为 2 批；过滤后长度 4 切为 2 批
（投影 + 聚合各计一次，故 downstreamBatches=4）。

### 4. `./run.sh examples/request_group_agg.json`（退出码 0）

```text
{status=NEW,  count(*)=3, count(amt)=3, sum(amt)=1000, avg=333.333..., min=100, max=600}
{status=PAID, count(*)=2, count(amt)=0, sum(amt)=null, avg=null, min=null, max=null}
{status=null, count(*)=1, count(amt)=1, sum(amt)=400,  avg=400.0, min=400, max=400}
```

PAID 两行 amt 均为 NULL；status 为 NULL 的行自成一组。

### 5. 导出：`./run.sh examples/request_export.json`（退出码 0）

```text
out/export_demo/table.json
out/export_demo/plan.json
out/export_demo/result.json
```

### 6. 错误请求（均退出码 1 / HTTP 400）

```text
selection 含越界下标 9（3 行表）:
  {"ok":false,"error":"无效选择下标 9：必须落在 [0, 2]（表共 3 行）"}
畸形 JSON '{oops':
  JSON 解析失败: 对象键必须是字符串（位置 1）
过滤引用不存在的列 x:
  {"ok":false,"error":"表 t 中不存在列 \"x\"；可用列: [id]"}
```

## 三、HTTP 服务实际链路

命令：`java -cp target/classes vecq.Main serve --port 8099 examples/table_orders.json`

```text
GET  /health                                   -> 200 {"ok":true,"service":"vecq"}
GET  /tables                                   -> 200，列出 orders(6 行, 列 id/amt/status)
POST /query (request_by_table_name.json)       -> 200 selected=[0,2,5], count(*)=3, agree=true
POST /query {"table":"orders","selection":[999]} -> HTTP 400 + 错误对象
POST /query (request_union_duplicates.json)    -> 200 selected=[0,1,1,2,4]
```

## 四、开发过程中出现过、已修复的未通过项（如实记录）

以下问题在开发自测中出现，最终版本均已修复并有对应回归断言；最终干净构建 0 失败。

1. **列构建缺陷（核心）**：最初只在显式提供 `nullRows` 时才创建 NULL 位图，
   导致仅用 `values` 中的 `null` 表达 NULL 时报"两种表达不一致"。
   修复为以 `values` 中的 null 构建位图、两种表达并用时校验集合一致
   （`Column.mergeNulls`），`ColumnTableTest` 覆盖两种表达方式。
2. **分组键 NPE**：分组聚合用 `List.copyOf` 构造不可变 key，而分组列允许 NULL，
   JDK 不可变集合拒绝 null 元素导致 NPE。改为普通 `ArrayList` 持有键
   （`ProjectAggregate.grouped`），`AllNullColumnTest` / `ProjectAggTest` 覆盖。
3. **测试侧误用 `List.of`**：`List.of` 不允许 null，含 null 的期望列表改为
   `Arrays.asList`（生产代码未使用该构造承载 null）。
4. 测试期望值修正：`id != 20` 在 3VL 下不命中 id=20 行与 NULL 行，期望改为
   `[0,2,4,5]`（引擎行为本身正确）。
5. 其它编译期问题：一处 `groups.add(...)` 误写（应为 `put`）、
   `Sum` 累加器 `seen` 未置位、测试 JSON 括号错误等，均在编译/首跑阶段修复。

## 五、已知限制

- 单线程内存引擎，无 JOIN / ORDER BY / LIMIT / 索引 / 优化器（不在需求范围）。
- 整型为 32 位；字符串仅支持等值比较；`avg` 返回 double。
- `javac -Xlint:all` 仅有 5 条自定义异常缺少 `serialVersionUID` 的序列化提示，无功能影响。
