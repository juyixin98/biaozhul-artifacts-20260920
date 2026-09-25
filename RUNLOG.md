# RUNLOG — 实际运行记录（如实记录）

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic x86_64，go1.22.2（无外部依赖，标准库实现）
- 原则：本文件只记录**实际执行过**的命令与真实输出结论；失败项和中途返工一并保留。

## 1. 最终验证结果（全部通过）

```bash
gofmt -l .            # 无输出（格式通过）
go vet ./...          # PASS
go test -race -count=1 ./...
go test -cover ./...
go build ./...        # PASS
```

`go test -race` 结果：

```
ok  counterreset/internal/httpapi   (race)
ok  counterreset/internal/rate      (race)
ok  counterreset/internal/series    (race)
ok  counterreset/internal/store     (race)
ok  counterreset/internal/wal       (race)
```

覆盖率：

| 包 | 覆盖率 |
|---|---|
| internal/series | 100.0% |
| internal/rate | 94.1% |
| internal/store | 88.2% |
| internal/httpapi | 76.8% |
| internal/wal | 76.4% |
| cmd/* | 无单测（入口/打印工具，由端到端演示覆盖） |

顶层测试 33 个全部 PASS（`go test -v` 清单见本节末；未使用 t.Parallel 外的跳用，
无 SKIP）。要点覆盖：负值/NaN/Inf/坏时间戳拒绝、整批原子拒绝、乱序排序、
同时间戳 max-wins、单次/连续两次重置、缺样标记、none/linear/clamped 三种边界策略、
容量条件上界、持平≠重置、少样本、非法窗口、+Inf→null、空集合→[]、
WAL 追加/重放/损坏尾行、HTTP 400/404/405 路径、模拟重启重放。

## 2. 实际端到端运行

### 2.1 手算对照工具

```bash
go run ./cmd/handcheck -capacity=100   # 输出存档 docs/handcheck-output.txt
go run ./cmd/handcheck -capacity=0     # 验证未提供容量时 upper=null
```

实际输出关键行：

- 序列 A（0,10,20,30,50,60,70,80,90,100；v=0,10,20,20,30,40,45,60,10,15）：
  `下界点估计 = 75；条件上界(C=100) = 915；速率下界 = 0.7500/s；重置 1 次；缺样 1 个`
- 序列 B（v=0,10,2,8,1,5）：
  `下界点估计 = 23；条件上界 = 505；速率下界 = 0.4600/s；重置 2 次`
- `-capacity=0`：两条序列均输出 `未提供容量 → 真实增量无上界（upper=null）`

与 README §5 的纸笔推导逐项一致。

### 2.2 HTTP 服务 + 重启持久化

```bash
./scripts/demo.sh    # 输出存档 docs/demo-output.txt；退出码 0
```

脚本自动挑选空闲端口，真实完成 15 个步骤，关键实测结果：

1. 首次启动加载合成种子 2 条记录；`/v1/series` 显示主序列 11 个唯一样本
   （累计 1 个同值重复、1 个冲突）。
2. 主序列完整窗口 `none`：`increase={point:75,lower:75,upper:null}`，
   `rate=0.75`，resets=1（60→10），gaps=1（30→50，2.0×中位间隔），coverage=1。
3. 同窗口 `capacity=100`：`upper=1015`（种子为 11 点序列，与 handcheck 的
   10 点序列 A 不同；Σ(100−v1+v2)=1015，手工复算一致），rate upper=10.15。
4. 窗口 [0,200]（后半无样本）：
   - none：point=75，coverage=0.5，factor=1，rate=0.375；
   - linear：factor=2，point=150，rate=0.75；
   - clamped：factor=1.05，rate=0.39375。
   注：种子主序列是 11 点（t=0..100 中 t=10,20,…,100，间隔均为 10s），
   与 handcheck 的 10 点序列 A（t=30→50 有 20s 缺口）不是同一条；
   序列 A 在同窗口的 clamped 因子为 1.05556（point=79.167，rate=0.39583），
   README §5 表格按序列 A 给出，两处分别自洽。
5. 乱序 + 重复 + 冲突摄入：归一化后 ts=20000 保留 99（max-wins），
   批次内 2 个冲突；查询 point=119。
6. 负值摄入 → **HTTP 400**，返回 rejected 明细（index 与中文原因）；
   使用样例文件 `examples/ingest-negative-rejected.json` 同样 400。
7. 非法 JSON → 400；不存在序列 → 404；错误方法 → 405。
8. `examples/ingest-reset.json` 文件摄入成功（5 样本），查询窗口 [10,40]
   point=22（13 + 3[重置 25→3] + 6），rate=0.7333/s，1 次重置。
9. kill 服务后重启：WAL 重放成功，手动摄入的序列数据仍在、顺序正确。

## 3. 开发过程中真实发生的失败与返工（未通过项记录）

以下问题都在开发过程中实际出现过，最终版本均已修复并有回归测试：

1. **首轮 `go test` 多个用例失败 —— 原因是我写测试期望时手算错误，代码输出正确。**
   例如序列 A 的下界合计被我误算成 85（实际 75）；条件上界先后误写成
   725/815（实际 915，Σv1=235 而非 425）。通过逐区间在纸上复算后更正测试，
   并保留算式注释防止再错。教训：手算基准必须独立于实现重新推导。
2. **clamped 外推规则第一版实现有误。** 我最初误用“中位间隔 + span<median
   走半间隔”的规则，与 Prometheus 的实际算法（平均间隔、1.1 倍阈值、
   超阈值端只补半个平均间隔）不符。测试因子（期望 1.5 实际 2.1）暴露后，
   重写 `clampedFactor` 并新增远边界截断用例 `TestComputeClampFarEdgeRule`。
3. **store 去重统计把历史样本混入新批次计数。** 首版合并后统一 Normalize，
   无法区分“批次内重复”与“与历史重叠”。重构为：先归一化批次，再与已存储
   时间戳比对，响应拆分为 `batch_duplicate_*` / `overlap_*` / `total_*`。
4. **空的 resets/gaps 被序列化成 `null` 而非 `[]`。** 端到端输出核对时发现，
   修复为初始化空切片，并在 `TestComputeFlatCounterNoReset` 加 JSON 形态断言。
5. **演示脚本首次运行全部 404。** 原因：硬编码端口 18080 被机器上无关进程
   `vccsim` 占用（curl 打到的是对方的 404，本服务 bind 失败已退出）。
   修复：脚本启动前用临时 socket 自动探测空闲端口（可用 `PORT` 覆盖）。
6. 小问题：曾在 store.go 文件头误输入两行无效文本、main.go 误用
   `syscall.EINVAL` 判断 EOF、测试里出现 `:=` 重复声明和非法 Go 表达式
   `x == nil && 0 || *x`——均由编译/vet 立即发现并修复。
7. `examples/*.json` 最初加了 `_comment` 说明字段，但服务端
   `DisallowUnknownFields` 会先拒绝它；已移除注释（说明移到 README），
   保证样例文件可直接 `--data @file` 使用。

## 4. 未做的事项 / 已知局限（明确声明）

- 无前端（按要求纯后端）。
- WAL 无分段、快照、保留策略与压缩，会线性增长；无集群/复制/两阶段提交。
- 条件上界依赖“每区间至多一次重置、无隐藏回绕”的显式假设；无容量时
  不给上界（`upper=null`），不以任何经验值替代。
- `linear`/`clamped` 的窗口外推是启发式估计，所有相关响应都带
  `extrapolation_factor`、`observed_coverage` 与 notes 提示，未声称精确。
- 时间戳为毫秒整数；未实现标签的正则/集合选择器（精确标签匹配即可满足样例）。

## 5. 复现实验的标准命令集

```bash
go test -race -count=1 ./...
go test -cover ./...
go run ./cmd/handcheck -capacity=100
go run ./cmd/handcheck -capacity=0
./scripts/demo.sh
go run ./cmd/counterreset -addr=:8080 -data-dir=./data -seed=examples/seed.jsonl
```
