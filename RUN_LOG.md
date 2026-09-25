# 运行记录（如实）

环境：Linux 6.8、Python 3.12.3，仅标准库。以下命令与结果均为本机实际执行所得。

## 复现命令

```bash
./run_tests.sh                                              # 全部验收
python3 -m unittest discover -s tests -p 'test_*.py' -v     # 仅单元/集成
python3 -m lattlang.cli check <program.lat>                 # 三端等价
python3 -m lattlang.cli analyze|ssa|optimize|run <file>
python3 -m lattlang.cli serve examples/requests/check.json  # JSON 服务
```

## 最终结果

* 单元/集成/模糊测试：**87 个全部通过**（`python3 -m unittest discover ...`）。
* 9 个验收样例 `check`：**三端（AST / SSA IR / 优化后 IR）输出与错误全部一致**。
* 安全抽查：可达 `1/0` 与动态除零仍在**同一源码位置**抛 “division or modulo
  by zero”；不可达 `1/0` 随死分支删除且正常输出。
* JSON 服务 6 个请求样例（含 1 个预期 `ok:false` 的解析错误）全部符合预期。
* 差分模糊：最终一轮 **5100 个随机程序（30 个种子）零行为分歧**；默认测试中
  固定种子运行 250 个。

## 验收反例的优化前后可观察行为

| 程序 | AST | 优化后 IR | 说明 |
|---|---|---|---|
| `unreachable.lat` | `1` | `1` | 常量真分支，else（print 999）删除 |
| `zero_trip_loop.lat` | `10` | `10` | `while 0` 循环体删除 |
| `loop_materializes.lat` | `0 1 2 99` | `0 1 2 99` | 循环携带值由常量变 ⊥，循环保留 |
| `confluence.lat` | `1` | `1` | 运行时未知条件，两边都活，合流 meet(1,2)=⊥ |
| `side_effects.lat` | `3 4 9` | `3 4 9` | print 顺序不变，死分支 print 删除 |
| `divzero_reachable.lat` | 除零错误(line 4:9) | 同位置除零错误 | `div 1 0` 不折叠、不删除 |
| `divzero_dynamic.lat` | 除零错误(line 4:9) | 同位置除零错误 | 除数运行时为 0，指令保留 |
| `divzero_unreachable.lat` | `5` | `5` | 死分支内 `/0` 随块删除，不抛错 |
| `mod_negative.lat` | `-3 -1 -3 1` | `-3 -1 -3 1` | 向零截断语义 |

## 开发过程中实际发现并修复的缺陷（由测试/模糊暴露）

按出现顺序，均有对应测试防回归：

1. **出口块漏排**：块排序 DFS 提前跳过 exit，导致分析时缺标签。修复排序逻辑。
2. **给一次性临时名插 φ**：`$tN` 本就单定义，不应插 φ；限定只对用户变量插。
3. **SSA 版本号冲突**：以“栈深度”命名会让合流点 φ 与不支配它的兄弟块定义撞号
   （`x.2 defined twice`）；改为每变量单调递增、不复用版本号，并在重命名前
   预注册全部名字的隐式 0 栈。
4. **符号/操作码映射用反**：IR 下降误用 opcode→符号表，改用 `SYMBOL_TO_OPCODE`。
5. **删块后 φ 槽位错位**：先删块再按新前驱重建 φ，会把回边值错配到错误前驱
   （`KeyError: 'x.3'`）；改为**删块前**按旧前驱顺序选择存活参数。
6. **SCCP 回边调度缺陷（核心）**：块首次访问后若再获得一条新的可执行入边，
   原实现不会重新入 flow，导致经回边到达的新值不参与 φ meet，循环变量被错误
   固定为入口常量 0（差分模糊中表现为优化后输出/除零位置变化）。修复：
   `add_edge` 在“已访问块得到新可执行入边”时重新入队以重算 φ。增加针对性
   回归测试 `test_back_edge_phi_remeets_when_edge_late`。

未通过项：开发中途 1–5 与 6 均曾导致测试/模糊失败，修复后当前主干全部通过；
当前没有已知未通过测试。
