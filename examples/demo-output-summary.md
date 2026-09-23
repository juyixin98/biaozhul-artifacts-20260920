# 实际运行输出摘要（scripts/demo.sh）

- 运行时间：2026-09-23
- JDK：Temurin OpenJDK 17.0.20.1+1（x86_64 Linux）
- 数据：5 个分片，每片 4 行（手工构造的极小分片，专用于功能验收）
- 全部 4 个查询响应中 `consistentWithFullScan = true`（裁剪与全扫描结果完全一致）

## 查询 1：amount EQ 10

边界相等（shard1 非空值全为 10）必须命中；shard3 全 NULL 不命中；
shard4 统计缺失被**强制扫描**但不命中。

```
一致: True  节省字节: -639 (-144.57%)
裁剪: 命中 2 行, 扫描分片 2/5, 读取 1081 字节 (stats 901 + data 180)
全扫: 命中 2 行, 扫描分片 5/5, 读取 442 字节
  shard 0: 裁剪  pruned:no_range_overlap   matched=0
  shard 1: 扫描  scan:range_overlaps       matched=2
  shard 2: 裁剪  pruned:no_range_overlap   matched=0
  shard 3: 裁剪  pruned:all_null_column    matched=0
  shard 4: 扫描  scan:stats_missing        matched=0
聚合(裁剪): cnt=2, cnt_amount=2, total=20, avg_amount=10.0, min=10, max=10
```

## 查询 2：amount EQ 0（核心验收：NULL 不得当成 0）

命中 0 行；`SUM/AVG/MIN/MAX` 全部为 **null 而不是 0**；只有统计缺失片被扫描。

```
一致: True
裁剪: 命中 0 行, 扫描分片 1/5（仅统计缺失的 shard4）
聚合(裁剪): cnt=0, cnt_amount=0, total=None, avg_amount=None, min=None, max=None
```

## 查询 3：amount GE 4

shard0 的 `max == 4` 是边界相等，必须扫描并命中 amount=4 的 1 行；
统计缺失的 shard4 强制扫描并命中 4 行；全 NULL 片裁剪。

```
一致: True
裁剪: 命中 11 行, 扫描分片 4/5
  shard 0: 扫描  matched=1   （边界相等行 amount=4 计入）
  shard 1: 扫描  matched=2
  shard 2: 扫描  matched=4
  shard 3: 裁剪  pruned:all_null_column
  shard 4: 扫描  matched=4   （统计缺失，强制扫描）
聚合(裁剪): cnt=11, total=1284, min=4, max=400
```

## 查询 4：无过滤

`count(*)=20` 但 `count(amount)=14`（6 个 NULL：shard1 有 2 个、shard3 有 4 个），
SUM=1290。NULL 行计入 COUNT(*)，但不 COUNT 列、不参与 SUM/AVG/MIN/MAX。

```
一致: True  节省字节: 0（无过滤时不裁剪，两种模式读取量相同）
聚合(裁剪): cnt=20, cnt_amount=14, total=1290, avg_amount=92.1428571429, min=1, max=400
```

## 关于“节省字节为负”

这是**如实测量**的结果，不是 bug：演示夹具每片只有 4 行，数据列文件本身极小
（单片 amount 列仅 49 字节左右），而裁剪模式需要额外读取 5 个 JSON 统计文件
（约 900 字节总计），统计读取成本超过了跳过 2~3 个微型分片省下的数据量。

- 裁剪的**正确性**不受影响：裁剪与全扫描结果始终一致，被裁剪分片数据读取为 0 字节。
- 真实分片通常有成百上千行，数据列远大于统计文件，此时裁剪有显著正收益。
  自动化测试中使用每片 64/128 行的夹具验证了正收益（见 `src/test/java/colscan/Tests.java`
  中“裁剪读取字节数严格小于全扫描”断言）。
