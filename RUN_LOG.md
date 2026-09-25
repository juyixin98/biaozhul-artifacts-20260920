# 运行记录（RUN LOG）

本文件如实记录本项目实际执行过的命令、结果，以及开发过程中**失败过的项与修复**。
日期：2026-09-23 ~ 2026-09-24。无前端。

## 环境

```text
OS:    Linux 6.8.0-90-generic
Python 3.12.3
NumPy 2.5.3
pytest（环境内已安装）
```

依赖只有 NumPy：`pip install -r requirements.txt`。

## 最终测试结果

命令：

```bash
python -m pytest tests/ -q
# 等价地：
python -m unittest discover -s tests
```

结果（**全部通过**）：

```text
pytest:  51 passed, 9 subtests passed in 14.01 s
unittest: Ran 51 tests in 14.379 s — OK
```

测试模块：

| 文件 | 覆盖 |
|---|---|
| `tests/test_validation.py` | 零尺寸/负尺寸/容差拒绝、零条带宽、NaN/Inf、超宽拒绝、等宽与容差边界、空列表、缺字段、类型错、n=301 超规模、布尔值不接受为数字 |
| `tests/test_bounds.py` | 面积下界数值、满铺情形、最大件高界、宽件界（W/2 边界严格性）、组合界取 max、40 个随机算例下界 ≤ 可行布局 |
| `tests/test_collision.py` | 区间/矩形共边共角不碰撞、正重叠必报、批量碰撞对、启发式布局在手工例与 120 个随机算例上合法、人为叠放/出界/负坐标必被检出 |
| `tests/test_exact_vs_bruteforce.py` | 精确 DFS 与**独立网格暴力法**在两件矩形全组合（W=1..5, w=1..W, h=1..3）、手工例、60 个随机小算例上最优高度一致；夹逼关系 LB ≤ OPT ≤ 启发式；精确器规模/非整数时正确 skipped |
| `tests/test_api.py` | JSON 成功/失败响应结构、exact 字段、JSON 可序列化、200 个随机小算例实证启发式存在次优且永不低于最优、CLI 退出码 0/1/3 |

> 说明：`pytest` 输出 "9 subtests" 来自参数化的 `subTest` 循环（被 parametrize 的
> 穷举/手工算例）；独立测试函数共 51 个。

## CLI 实际输出（验收点）

```bash
$ python -m packing.cli examples/request_error_zero_size.json ; echo $?
{"status":"error","error":{"code":"bad_rectangle_size",
 "message":"rectangles[0] 含零或负尺寸：width=0 height=4（容差 1e-09）"}}
1

$ python -m packing.cli examples/request_error_too_wide.json ; echo $?
{"status":"error","error":{"code":"rectangle_too_wide",
 "message":"rectangles[0] 宽度 12 超过条带宽度 10（矩形不允许旋转）"}}
1

$ echo '{bad' | python -m packing.cli ; echo $?
{"status":"error","error":{"code":"invalid_json","message":"请求不是合法 JSON: ..."}}
3
```

成功路径：`python -m packing.cli examples/request_example.json` → `status=ok`，
见 `examples/response_example.json`；该例下界 8.4、启发式 11，
用精确求解器验证**真实最优 = 10**，因此该样例本身就是一个"启发式非最优"的实证。

`compute_exact` 样例：`examples/response_exact_small.json`，启发式与精确最优均为 5，
且 `gap.proven_optimal = true`（启发式高度达到有效下界，间接证明最优）。

## 性能实测（本机）

```text
n=300（上限规模，W=50，矩形 1..50 随机）启发式全流程（含校验+布局校验）：约 1.64 s
精确求解器 n=8 的 30 个随机算例（固定种子 1，可复现）：
    多数 < 2 s；最慢约 37.8 s（W=12，OPT=29）
```

精确求解器仅用于 `compute_exact=true` 的小整数验证算例，不在常规请求路径；
搜索超 300 万节点会保守返回 `exact.status="skipped"`，不声称最优。

## 开发中实际出现过的失败与修复（如实记录）

精确求解器迭代了多个版本，早期版本**确实给出过错误答案**，均由测试（尤其是与
独立网格暴力法的对照、以及"启发式高度 < 精确器声称的最优"这一夹逼不变量）抓出：

1. **候选点只取已放矩形的右边/上边 → 漏解。**
   现象：算例 `W=4, [(4,6),(2,4),(1,6),(1,3),(3,1),(1,6)]` 中，启发式给出合法的高 13
   布局，而精确器错误地声称最优 14。
   定位：该布局中某矩形的左下角 x=1 来自另一件（更晚才会涉及的）竖条的**左**边，
   仅取右/上边会漏掉。修复尝试：加入左/下边。

2. **改为按放置顺序收集全部四边仍漏坐标 → 仍漏解。**
   原因：需要的边坐标可能属于"当时尚未放置"的矩形，局部、随放置增长的候选集
   仍不保证含目标坐标。

3. **改用全局子集和坐标后，又加了"每件必须有左/下支撑"的稳定性剪枝 → 仍误删可行解。**
   根因定位（写在分析脚本中逐件核对）：固定放置顺序（高降序）下，
   竖条 (1×6) 要放在 x=2，但其左支撑件（2×4）在该顺序里**尚未放置**，
   于是被局部支撑判定拒绝。"每个最终解都可压缩为稳定布局"在数学上成立，
   但贪心放置顺序 + 局部支撑判定**不保证能抵达**该稳定解。

4. **改用"bottom-left 强制格"（最低最左空格必须被覆盖）→ 在允许空洞的条带装箱中不完备。**
   该规则对"必须铺满"的装箱成立，但条带装箱允许留白：一个宽矩形可能浮在一个
   永远无法被更小矩形填满的空格上方，最低空格可以合法地留空。此版甚至把上面
   已知可解的算例报成 `infeasible`，立即被同一测试抓出后弃用。

5. **最终方案（当前版本）**：回到数学上可证明完备的**全局子集和坐标**，
   只保留不影响完备性的加速——位掩码网格碰撞、同形状件去重、MRV
   （可放置位置最少的剩余形状先放，0 个位置立即剪枝）、记忆化、面积剪枝、
   对高度二分搜索。正确性由完全独立、无任何剪枝的网格暴力法在全部小算例上对照确认。
   代价是 n=8 某些异质算例较慢（实测最慢约 38 s），但结果正确，且可选、限验证用途。

另修复过两处实现错误：`_recover_layout` 中一处矛盾的越界条件表达式；
以及同尺寸对称剪枝最初用跨回溯可变数组记录"上一件位置"（在回溯兄弟分支间会串状态），
改为通过 DFS 参数传递字典序下界。

## 已知限制

* 精确求解器仅支持整数尺寸、`n ≤ 8`、宽/坐标 `≤ 40`；其余算例返回
  `exact.status="skipped"`（带原因），不影响启发式与下界主结果。
* 启发式是可行上界，**不保证最优**；响应中 `heuristic.guarantee` 固定为
  `"heuristic (not proven optimal)"`，代码任何路径都不把它标为全局最优。
* 下界是三个经典有效界的组合，并不总是紧（存在 LB < OPT 的算例）；这是下界方法的
  固有特性，已在 README 与响应 `gap.note` 中说明。
* 纯后端：无 HTTP 服务与界面；`packing.api.solve` 是不绑框架的纯函数，CLI 为
  `python -m packing.cli`，便于嵌入任意 Web 层。
