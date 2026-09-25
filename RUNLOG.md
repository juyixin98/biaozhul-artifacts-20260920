# 运行记录（RUNLOG）

环境（`python3 --version` / NumPy）：

- Linux 6.8.0-90-generic，Python **3.12.3**
- NumPy **2.5.3**，pytest/pytest-cov 已安装
- 工作目录：项目根；所有命令默认在根目录执行，包通过 `PYTHONPATH=src` 导入，无需安装

> 本文件记录的全部输出均为上述环境中实际运行所得。未通过 / 返工项见第 5 节。

## 1. 自动化测试

命令：

```bash
PYTHONPATH=src python -m pytest tests/ --cov=src/tmem --cov-report=term-missing
```

实际输出（结尾）：

```
................................................................         [100%]
================================ tests coverage ================================
_______________ coverage: platform linux, python 3.12.3-final-0 ________________

Name                   Stmts   Miss Branch BrPart  Cover
----------------------------------------------------
src/tmem/__init__.py       7      2      0      0    71%
src/tmem/cli.py           53      6     12      5    83%
src/tmem/dag.py          186      7    108      7    95%
src/tmem/executor.py      98      1     26      4    96%
src/tmem/planner.py      224      7     64      5    96%
src/tmem/service.py       36      2     10      2    91%
----------------------------------------------------
TOTAL                    604     25    220     23    94%
65 passed
```

结果：**65 个测试全部通过，语句+分支覆盖率 94%（核心模块 dag/planner/executor 95%/96%/96%）**，满足 ≥80% 要求。

> 注：上面数字为加入健壮性与零尺寸回归测试后的最终值；开发中途首次全量通过时为 58 个测试、同样 94% 覆盖率。

测试构成：

- `test_dag.py`：形状推导（add 广播、向量/矩阵/批量 matmul、slice、链式 slice）、校验错误路径。
- `test_planner.py`：last-use 生死、多消费者存活、槽位复用、理论峰值下界、逐执行点窗口不相交、视图别名家族、链式视图全局偏移、预算硬/软执行。
- `test_executor.py`：两执行器逐输出 `allclose`、单 arena 分配数、广播/批量/标量、合成数据可复现。
- `test_fuzz.py`：**200 个随机 DAG** 模糊测试（随机形状/广播/视图/多消费者/分支），另含一个结构性断言确认模糊确实生成了视图与多消费者图。
- `test_service_cli.py`：4 个样例端到端、报告 JSON 可序列化、预算失败、CLI（进程内 + 子进程 + stdin）。

## 2. 扩展随机模糊（1000 图）

在内置 200 个之外，另跑了一次性脚本（1000 个随机 DAG，种子 424242，随机输入种子）：

```text
1000 random DAGs: all safe and numerically equivalent
```

即 1000 个图全部满足：无同时存活的重叠窗口、切片视图不越界、两执行器输出一致。该脚本未固化进仓库（属一次性验证）。

## 3. 四个验收样例（实际输出）

命令：

```bash
for f in multi_consumer shared_view branch_merge peak_budget_chain; do
  PYTHONPATH=src python -m tmem.cli examples/$f.json \
      --save-report reports/${f}_report.json --quiet
done
```

实际输出：

```
tensors=9  no_reuse_peak=60el  reuse_peak=44el  saved=26.667% safety=OK numeric_match=OK   # multi_consumer
tensors=11 no_reuse_peak=93el  reuse_peak=65el  saved=30.108% safety=OK numeric_match=OK   # shared_view
tensors=13 no_reuse_peak=118el reuse_peak=64el  saved=45.763% safety=OK numeric_match=OK   # branch_merge
tensors=10 no_reuse_peak=640el reuse_peak=192el saved=70.0%   safety=OK numeric_match=OK   # peak_budget_chain
```

（`el` = float32 元素数；每行上方注释为对应文件名，便于对照。）

完整报告已落盘 `reports/*_report.json`。关键验证点摘录：

- **多消费者**（`multi_consumer`）：分配时间线中 `t1`（p5 出生）的 `last_use=7`（第二个消费者 `t3`），在 p7 仍占窗口；`a,w` 在 p6 被回收复用，`b` 在 p7 被回收。
- **共享视图**（`shared_view`）：根 `X` 的别名家族为 `['X','v','v2','v3']`，因 `v3` 是输出，家族存活到最终点 10；`t5` 复用了已死亡权重 `W` 的窗口 `[24,30)`。三个输出逐元素 `exact_equal=true`（max_abs_diff=0.0）。
- **分支汇合**（`branch_merge`）：两路切片在 `m1`(add) 汇合、两路 matmul 在 `m2`(add) 汇合，峰值下降 45.763%，数值一致。
- **峰值预算**（`peak_budget_chain`）：8 段 8×8 matmul 链，arena=192 恰为理论下界（输入权重 64 + 链上相邻两活跃张量 128），`enforce_budget=true` 下退出码 0。

## 4. 边界 / 错误路径（实际输出与退出码）

```text
$ echo '{"inputs":{},"ops":[],"outputs":[]}' | PYTHONPATH=src python -m tmem.cli -
error: invalid request: 'inputs' must be a non-empty object name->shape
exit=2

$ PYTHONPATH=src python -m tmem.cli examples/peak_budget_chain.json --enforce-budget --quiet
tensors=10 no_reuse_peak=640el reuse_peak=192el saved=70.0% safety=OK numeric_match=OK
exit=0

$ echo '{not json' | PYTHONPATH=src python -m tmem.cli -
error: invalid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
exit=2

$ # 将 multi_consumer 预算改为 10 并硬执行：
$ python3 -c "import json;r=json.load(open('examples/multi_consumer.json'));r['peak_budget_elements']=10;print(json.dumps(r))" \
    | PYTHONPATH=src python -m tmem.cli - --enforce-budget
error: budget: peak budget exceeded: arena needs 44 elements (176 bytes), budget 10 elements (40 bytes)
exit=2
```

退出码约定：0 成功；2 请求非法 / JSON 非法 / 文件缺失 / 预算超限。

## 5. 开发中实际出现的失败与修复（如实记录）

以下为开发过程中真实遇到、已修复的问题：

1. **`as_strided` 单位错误导致 NaN/垃圾值**：复用执行器最初用 `np.lib.stride_tricks.as_strided` 手算视图偏移，调试时一度把元素单位的步长当字节传入，出现 `nan` 与跨槽位垃圾值（如 `-5.37e30`）。最小复现确认 `as_strided` 的 strides 单位是**字节**后，进一步决定彻底弃用手算偏移，改为"对 env 中源数组直接做 NumPy 切片"——源本身已是同一 arena 的视图，链式切片自动正确别名。修复后所有数值比对通过（代码中已无任何 `stride_tricks` 调用）。
2. **多跳切片 end_offset 公式错误**：`_view_info` 初版对第二跳切片的坐标用了错误的累加方式；改为标准链式坐标平移（每跳区间相对当前视图原点，root→叶逐跳相加），并由 `test_chained_view_global_offsets` 锁定 `x[1:5,2:7][1:3,0:4] → 根坐标行[2:4)、列[2:6)`。
3. **测试自身若干计数/辅助错误**（非产品缺陷）：pytest 参数名 `request` 与 fixture 冲突 → 改名 `payload`；`_req(inputs={})` 被 `or 默认值` 吞掉空字典 → 改哨兵；一处分配数期望 5 实为 6；一处视图重建测试从错误起点 reshape → 改为从根窗口起点 reshape 再切片。
4. **遗留死代码清理**：删除未使用的 `iter_dependencies`、未用导入、占位 `_Slot.free_at` 字段等；每步清理后重跑全量测试确认无回归。

### 5b. 独立代码审查发现并修复的问题

实现完成后启动了两个独立审查代理（正确性、安全/边界）。安全/边界审查确认并已全部修复：

| 级别 | 问题 | 修复 |
|------|------|------|
| HIGH | `seed` 为 `"abc"`/`[1]` 等时未捕获异常，退出码 1 | service 层校验整数类型，非法返回退出码 2 |
| HIGH | 顶层非字典请求 + `--enforce-budget` 时 `{**request}` 先崩 | CLI 读取后先校验为对象再展开 |
| HIGH | 无张量尺寸上限，广播可爆炸到 1e10 元素触发 NumPy 分配异常 | 增加单维 ≤1e6、单张量 ≤1e8 元素、≤1e4 算子上限，校验阶段拒绝 |
| MEDIUM | 规划 O(n²)（成对窗口校验 + 逐点全量扫描），4000 算子 3.8s | 改为按槽位分组的 O(n log n) 校验 + 出生/死亡事件扫描；**4000 算子降至 56.6ms，10000 算子 140.3ms** |
| LOW | 目录作为输入路径、坏的 `--save-report` 路径产生栈追踪 | 统一捕获 `OSError`，返回退出码 2 |

每个修复都配了回归测试或手工复现验证（6 条原始复现路径现全部干净返回退出码 2）。O(n log n) 校验器另在 3000 个随机图上与 O(n²) 暴力两两法逐一对照，结果完全一致；并用人为注入的重叠确认其能正确检出。

随后进行第二轮**正确性专项审查**（首个审查代理运行异常超时被终止，但其终止时的部分结论指向了一个真实问题，随后由我独立复现确认），发现并修复了两个**空切片（`start==stop`，产生 0 元素张量）相关的真实缺陷**：

| 缺陷 | 触发场景 | 修复 |
|------|---------|------|
| O(n log n) 校验器对**同偏移不同槽位占用者**只看时间、未看 size | 两个 0 元素根（由空视图经 matmul 产生）在无槽位释放时都新建容量 0 的槽位、共享同一 arena 偏移且生命周期重叠，被误判为存储冲突（`AllocationError`） | 同槽位占用者仅当双方 `size>0` 且时间重叠才算冲突（空窗口 `[o,o)` 永不触碰，与原窗口相交判定等价，不会漏报真冲突） |
| 空切片视图的扁平足迹 `end_offset` 用了 `(end-1)*stride` | 空轴时不存在"最后元素"，得到 `-1` 类错误偏移，视图足迹校验误判越界 | 空视图足迹记为起点处的空区间 `[first, first)` |

修复后：新增专项回归测试 `test_zero_size_roots_sharing_offset_are_not_conflicts`；模糊生成器以 12% 概率生成空切片，重跑 1000 个随机图，其中 **342 个含零维张量**，全部安全且两执行器数值一致（空输出 `shape=(0,k)` 正确）。

**当前状态：无已知未通过项。** 全部 65 个仓库测试通过，额外 1000+3000 个随机图（含 342 个含零维张量的图）验证通过；未覆盖的仅为少量防御性异常分支（见覆盖率 Missing 列）。

## 6. 复现实验步骤汇总

```bash
# 1) 测试 + 覆盖率
PYTHONPATH=src python -m pytest tests/ --cov=src/tmem --cov-report=term-missing

# 2) 四个样例（精简摘要 + 落盘完整报告）
for f in multi_consumer shared_view branch_merge peak_budget_chain; do
  PYTHONPATH=src python -m tmem.cli examples/$f.json \
      --save-report reports/${f}_report.json --quiet
done

# 3) 预算硬失败演示
python3 -c "import json;r=json.load(open('examples/multi_consumer.json'));r['peak_budget_elements']=10;print(json.dumps(r))" \
  | PYTHONPATH=src python -m tmem.cli - --enforce-budget; echo "exit=$?"
```
