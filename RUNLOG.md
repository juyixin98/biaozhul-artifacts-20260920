# 运行记录（RUNLOG）

环境：Linux x86_64，Python **3.12.3**，仅标准库（无第三方依赖，未安装/使用
任何编译器或解析器库）。所有命令在仓库根目录执行。本文件如实记录实际运行的
命令与结果；**没有未通过项**。

## 1. 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

结果（实际输出结尾）：

```
----------------------------------------------------------------------
Ran 96 tests in 1.36s

OK
```

96 个用例全部通过（`...` 全为点；`grep -c 'ok$'` 计数 96）。覆盖：

* 区间域单元测试（含无穷端点乘法符号、截断除/余、widen/narrow）；
* 在有限整数网格上对 `+ - * / %` 抽象算术做**逐点穷举**核对（加法/减法/
  乘法验证“最紧”，除法/余数验证“每个具体值都被包含”）；
* 词法位置、注释、优先级、非结合比较、非法程序、IR 降阶与位置；
* 守卫消警、扩大/收窄后的单层与**嵌套**循环不变量、possible/certain 区分、
  不可达块不报警、负下标、数组内容 Top、大整数数学精度；
* **穷举有界输入对照真实执行**的差分测试（安全守卫、未知界循环、负下标、
  取模、相关变量误报、数组内容误报）；
* **随机模糊**：自动生成标量/数组/循环（含嵌套循环）程序并穷举；
* JSON HTTP 服务的真实本地服务器端到端测试。

## 2. 穷举差分验证（验收主脚本）

命令：

```bash
python3 scripts/validate.py ; echo "exit=$?"
```

结果：`exit=0`，结尾打印

```
All programs satisfy the soundness contract over the bounded input regions.
(Spurious alarms above are the precision boundary of the non-relational interval domain.)
```

逐程序实际结果（输入区域、检查点数、真实崩溃站点→崩溃输入数、漏报 MISSED、
误报 spurious，全部 `RESULT: SOUND`）：

| 程序 | 有界输入区域 | 检查点 | 真实崩溃站点(#输入) | MISSED | spurious |
|---|---|---|---|---|---|
| safe_loop.ivl | 无 | 1 | — | [] | [] |
| correlation_loss.ivl | x∈[-15,15] | 31 | div_by_zero(31，每点必崩) | [] | [] |
| negative_index.ivl | 无 | 1 | index_oob(1，确定) | [] | [] |
| guarded_index.ivl | k∈[-20,20] | 41 | — | [] | [] |
| unknown_loop.ivl | n∈[-3,8] | 12 | index_oob(4，即 n=5..8) | [] | [] |
| modulo.ivl | a∈[-8,20],m∈[-3,8] | 348 | div_by_zero(12，a=0) | [] | [] |
| mixed.ivl | k∈[-20,20] | 41 | div_by_zero(1，k=0) | [] | [] |
| **spurious_correlation.ivl** | x∈[-30,30] | 61 | — | [] | **[div_by_zero]** |
| **spurious_array.ivl** | 无 | 1 | — | [] | **[div_by_zero]** |
| unknown_loop.ivl（安全区域） | n∈[0,4] | 5 | — | [] | **[index_oob]** |

后三行明确展示**保守性边界**：分析器报“可能”，但整个有界输入区域内没有任何
一次真实执行崩溃——分别来自非关系域的相关变量精度损失、数组内容用单一 Top
抽象、以及未知输入界定的循环无法恢复有限上界。

健全性结论：**所有真实崩溃站点都被报告（MISSED 全为空），所有退出值都落在
抽象区间内（invariant violations = 0）**。

## 3. 随机模糊（额外压力验证）

除固定用例外，临时把规模调大做过一次压力运行（随机种子可复现），随后把默认
规模固化进 `tests/test_fuzz.py`：

* 150 个随机标量程序 + 120 个数组下标程序 + 80 个（含嵌套）循环程序，
  共 **350 个随机程序**；
* 每个程序对其有界输入区域完整穷举真实执行；
* 结果：**0 个漏报（missed crash site）、0 个不变量违背**。

开发过程中该模糊器确实抓到过三个真实缺陷并已修复（这也是“穷举对照真实执行”
价值的体现）：

1. 区间除法只采样同侧角点，漏掉“小被除数/大除数”，导致 `100/[3,+∞)` 被错算
   成 `[33,33]`（应为 `[0,33]`）——改为端点+符号点的笛卡尔积并处理无界除数；
2. 截断余数上界只看除数，未考虑 `|a%b| ≤ |a|`，`a%[2,+∞)` 会漏值——改为
   `|rem| ≤ min(max|a|, max|b|−1)`；
3. 嵌套循环中内层头把外层计数器一起 widening、以及“新上界已是 +∞ 时反而
   不扩大”的 widening 判定错误——改为按自然循环体只扩大本循环修改的变量，
   并修正 widen 对无穷新界的处理。

## 4. 命令行与 JSON 服务（人工冒烟）

```bash
# 有界循环：循环头 i∈[0,10]，出口 i=10；100/(i-10) 判为“确定除零”
printf 'input var n;\nvar i=0;\nwhile(i<10){i=i+1;}\nvar y=100/(i-10);\n' \
  | python3 -m interval_ai.cli analyze -

# 具体执行（数学整数，截断语义）
python3 -m interval_ai.cli run examples/programs/modulo.ivl 0 3
```

服务端到端（`python3 -m interval_ai.service --port 8099` 后用 curl）：

* `GET /healthz` → `{"status":"ok"}`；
* `POST /analyze`（守卫程序）→ `alarms: []`；
* `POST /analyze`（`var z=9/k`，k 未知）→ 1 个 `div_by_zero/possible`，
  `location` 指向 `/`；
* `POST /run` inputs `[3]` → `z=3`；inputs `[0]` → `runtime_error.kind =
  div_by_zero`；
* 非法 JSON / 缺 `source` / 语法错误均返回 HTTP 400 与带位置的错误对象。

以上均与 `tests/test_service.py` 中的断言一致。

## 5. 未通过项

无。`python3 -m unittest` 退出码 0（96/96），`scripts/validate.py` 退出码 0。
