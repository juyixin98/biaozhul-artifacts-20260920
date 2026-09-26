# 运行报告（RUN_REPORT）

> 本报告如实记录开发过程中实际执行的命令、输出结果与未通过项。
> 环境：Linux 6.8.0、Python 3.12.3、NumPy 2.5.3、pytest 9.1.1（pytest-cov 7.1.0）。
> 所有数据为合成数据；未连接硬件、未启动网络服务、未制作任何前端。

## 1. 执行的命令与最终结果

| # | 命令 | 结果 | 退出码 |
|---|---|---|---|
| 1 | `python3 -m pytest tests/ -v` | **99 passed** | 0 |
| 2 | `python3 -m pytest tests/ --cov=transform_tree --cov-report=term-missing` | 99 passed，**总覆盖率 95%** | 0 |
| 3 | `PYTHONPATH=src python3 scripts/handcalc_check.py` | **手算复核全部 PASS**（10 项对比） | 0 |
| 4 | `PYTHONPATH=src python3 -m transform_tree examples/request.json -o examples/result.json` | 4 条查询成功、3 条按预期失败 | **1（设计如此）** |
| 5 | `PYTHONPATH=src python3 -m transform_tree examples/minimal_request.json` | 1 条查询成功 | 0 |
| 6 | `echo '{...静态...}' \| python -m transform_tree -`（stdin 模式，见测试） | 成功 | 0 |

命令 4 的退出码 `1` **不是缺陷**：样例请求中故意包含缺口、越界、未知帧查询，
用于验证错误会被逐条记录而不是崩溃；按约定存在失败查询时退出码为 1。
请求语法/建树类错误（坏 JSON、环、多父）退出码为 2。

## 2. 覆盖率明细（命令 2 原始输出）

```
Name                                   Stmts   Miss  Cover
src/transform_tree/__init__.py             6      0   100%
src/transform_tree/__main__.py             4      4     0%   (3 行转发垫片，被 -m 间接执行)
src/transform_tree/errors.py              11      0   100%
src/transform_tree/json_io.py            143      9    94%
src/transform_tree/main.py                62      7    89%
src/transform_tree/synthetic.py           11      0   100%
src/transform_tree/timed_sequence.py      73      1    99%
src/transform_tree/transform.py          120      4    97%
src/transform_tree/tree.py               115      2    98%
TOTAL                                    545     27    95%
```

95% 高于 80% 要求。未覆盖部分为 CLI 的 stdin 异常包装等少量防御分支。

测试分布（99 项）：

```
tests/test_transform.py           14   刚体变换、四元数、slerp、反射拒绝
tests/test_timed_sequence.py      13   插值、越界、缺口、重复时间戳
tests/test_tree_topology.py       11   环、多父、不连通、跨分支 LCA
tests/test_handcalc_acceptance.py  7   手算三层链/逆变换/插值/时间语义
tests/test_json_cli.py            11   JSON 解析、CLI 文件/stdin/退出码
tests/test_edge_cases.py          43   边界与错误输入分支
```

## 3. 验收点与实际结果

### 3.1 手算三层变换链（scripts/handcalc_check.py）

模型：`T_base_arm=(Rz90°, t=(1,0,0))`，`T_arm_wrist=(Rz90°, t=(0,1,0))`。

| 验收项 | 手算期望 | 库输出 | 结果 |
|---|---|---|---|
| 复合旋转 T_base_wrist | Rz180° | Rz180° | PASS |
| 复合平移 | Rz90°·(0,1,0)+(1,0,0)=(0,0,0) | (0,0,0) | PASS |
| 点 p=(1,0,0) 映射 | (−1,0,0) | (−1,0,0) | PASS |
| 逆变换旋转 T_wrist_base | Rz180° | Rz180° | PASS |
| 逆·正 | 单位矩阵 I | I（误差 <1e-10） | PASS |
| 单段逆 T_wrist_arm | Rz(−90°), t=(−1,0,0) | 同左 | PASS |
| t=1 旋转插值 | 复合 Rz90° | Rz90° | PASS |
| t=1 平移插值 | (0.1464466094, 0.3535533906, 0) | 同左（误差 <1e-10） | PASS |
| t=−0.5 / t=2.5 越界 | TimeNotCoveredError | 均抛出 | PASS |
| 3s 数据缺口（阈值 0.6s） | TimeGapError | 抛出 | PASS |

### 3.2 时刻缺口与"不得把最新值默认为历史值"

- 早于首帧（如 −0.1）与晚于末帧（如 10.5）均返回 `TimeNotCoveredError`，
  系统不会返回首/末帧值顶替（单测 `test_lookup_before/after_last_keyframe_is_rejected`、
  `test_history_is_not_filled_with_latest_value` 锁定此行为）。
- `examples/request.json` 中 link2 在 (4s, 6s) 人为断流 2 秒；t=5.0 查询返回
  `TimeGapError`；缺口之外（0.5s 采样间隔 ≤ 0.6s 阈值）正常插值。

### 3.3 旋转插值

四元数 slerp：端点精确、90° 姿态中点为 45°、对 `−q` 输入走最短弧、
1e-9 弧度小角度退化为归一化 LERP 且结果有限、单位范数（见 test_transform.py）。

### 3.4 拓扑校验

- 同一子帧挂第二个父帧 → `MultipleParentsError`（且冲突边不生效）；
- 自环、把祖先挂到后代下（2 层与 4 层深环）→ `CycleDetectedError`；
- 两个不连通根之间查询 → `FramesNotConnectedError`；
- 同一父帧重复添加同一条边视为幂等更新（不报多父）。

### 3.5 完整样例实际输出摘要（命令 4，examples/result.json）

```
overall ok: False | frames: 5 | edges: 4
[0] OK   tool -> base @ t=2.0   chain tool->link2->link1->base
        t=(0,0,-0.45)  q=(0.995004,0,0,-0.099833)   # Rz(-0.2)，逆方向 z=-0.45
[1] OK   base -> tool @ t=1.25                     # 插值角 0.125 rad
        t=(0,0, 0.45)  q=(0.998048,0,0, 0.062459)
[2] OK   sensor -> tool @ t=0.0  chain sensor->base->link1->link2->tool
        t=(0,0.5,0.15)  q=(0.707107,0,0,-0.707107)
[3] FAIL tool -> base @ t=5.0   TimeGapError         # 2s 断流
[4] FAIL tool -> base @ t=-0.1  TimeNotCoveredError  # 早于首帧
[5] FAIL tool -> base @ t=10.5  TimeNotCoveredError  # 晚于末帧，不外推
[6] FAIL tool -> gripper @ t=1  UnknownFrameError
```

最小样例（命令 5）：tip→base @ t=1，两帧 0s/2s 之间插值到 45°，
逆变换平移手算 `(-0.353553391, 0.353553391, -0.1)`，实际输出一致。

## 4. 过程中出现过的未通过项（如实记录，均已修复）

首次运行 `pytest` 时 **7 项失败 + 后续补测试时 2 项失败**，全部定位清楚：

**A. 测试期望/脚手架错误（库实现本身正确）—— 7 项：**
1. `test_slerp_midpoint_is_45_degrees`：我把 90° 姿态的中点误写成 22.5°，
   正确是 45°（四元数分量是半角）。库输出 45° 正确，改测试期望。
2. `test_slerp_takes_shortest_arc...`：同类半角笔误（60° 中点应为 30°）。
3. `test_keyframe_exact_hit_returns_pose`：断言漏加平移 `(3,2,1)`，
   期望应为 `(3,3,1)`。
4. demo 测试中 `tool->base` 是逆方向，z 平移应为 −0.45 而非 +0.45。
5. `test_history_is_not_filled_with_latest_value`：测试文件漏 `import pytest`。
6. `test_gap_between_bracketing_keyframes...`：测试数据正常段帧间隔 1s 也超过
   0.6s 阈值，改为 0.5s 正常采样 + 中间断流。
7. 手算脚本 `main()` 内 `FAIL_COUNT += 1` 缺少 `global` 声明，
   导致对比全 PASS 后结尾 `UnboundLocalError`（已修，退出码恢复为 0）。

**B. 补充边界测试时 —— 2 项：**
8. `(Transform.identity() == 42)`：误断言结果为 `NotImplemented`，
   Python 实际回退求值为 `False`，改断言。
9. （同轮）无库代码改动。

**C. 一处生产代码笔误，写后即时发现（在首次测试前已修）：**
- `main.py` 中 `except TransformTreeError as exc::` 双冒号语法错误，已直接修正。

结论：失败均为测试期望笔误/脚手架问题与一处语法笔误，**核心算法
（变换复合、逆、slerp、LCA 链路、时间覆盖/缺口判定）自首次可运行起输出即与
手算一致，未发现数值或逻辑缺陷**。修复后 99 项全部通过，复跑稳定。

## 5. 复现步骤

```bash
cd <项目根目录>
pip install -r requirements.txt
python3 -m pytest tests/ -q --cov=transform_tree --cov-report=term-missing
PYTHONPATH=src python3 scripts/handcalc_check.py
PYTHONPATH=src python3 -m transform_tree examples/request.json ; echo $?   # 预期 1
PYTHONPATH=src python3 -m transform_tree examples/minimal_request.json     # 预期 0
```
