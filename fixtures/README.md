# 最小数据夹具（fixtures）

全部为本地合成数据，无生产账号、无真实业务数据。CSV 第一行为表头；
空字段或未加引号的 `NULL` 按 SQL NULL 处理；整数列之外的取值按文本列处理
（本目录夹具均为 `int` 列）。目录名即场景名，文件名即关系名。

## triangle/ —— 三角查询 R(a,b) ⋈ S(b,c) ⋈ T(a,c)

与内置夹具 `triangle`（`src/dataset.rs`）逐行一致，用于对照 CSV 加载路径
与内存路径是否等价。

- `S` 中 `(3,1)` 重复两次、`T` 中 `(2,1)` 重复两次：验证多重性（multiset）。
- `R(4,5)`、`S(9,9)`、`T(7,7)` 为孤立边：不得出现在结果中。

手工推导的结果多重集（输出变量序 `a,b,c`）：

| a | b | c | 多重性 |
|---|---|---|--------|
| 1 | 2 | 3 | 1 |
| 2 | 3 | 1 | 4（S 重复 2 × T 重复 2） |
| 2 | 4 | 1 | 2（T 重复 2） |

共 7 行（3 个不同取值）。朴素全枚举需检查 5×5×4 = 100 个行组合。

## skew/ —— 高度倾斜（热键）

`R` 的 20 行中 19 行键为热键 `0`、1 行为 `1`；`S` 的键 `0..19` 各一行。
结果 20 行（k=0 时 19 行，k=1 时 1 行）。朴素两两计划需检查
20×20 = 400 个组合并先物化中间积；Leapfrog 只需个位数 seek。

## sparse/ —— 稀疏交集

`R.k = 0..9`，`S.k = {9} ∪ {20..29}`，交集只有单个键 `9`。
结果恰 1 行：`(9, 5009, -9)`。验证“跳跃不漏交集”：R 的迭代器直接
seek 到 999… 即 9，无需线性扫描。朴素需检查 10×11 = 110 个组合。

## nulls/ —— NULL 策略

`R(a,b)`、`S(a,c)` 各含一行 `a = NULL`。

- `null_policy=sql`（默认）：NULL 不参与等值连接，结果只有 `(1,10,100)`；
  两表各 1 行被 NULL 过滤（诊断计数 `null_rejected`）。
- `null_policy=match_nulls`：NULL 视为普通等值，结果追加 `(NULL,20,200)`。

## 加载方式

```rust
use leapfrog_triejoin::batch::ColumnType;
use leapfrog_triejoin::dataset::{load_csv, CsvColumn};

let schema = [
    CsvColumn { name: "a".into(), ty: ColumnType::Int },
    CsvColumn { name: "b".into(), ty: ColumnType::Int },
];
let (name, batch) = load_csv("R", &std::fs::read("fixtures/triangle/R.csv")?, &schema)?;
```

集成测试 `tests/csv_fixtures.rs` 逐条断言上述手工结果。
