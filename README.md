# 坐标变换时间树（Temporal Transform Tree）

纯后端的机器人**离线**坐标变换计算库：维护一棵随时间变化的刚体变换树，
支持在**任意指定时刻**查询**任意两个坐标系**之间的刚体变换。

- 语言：Python 3.10+，唯一运行期依赖 **NumPy**
- 数据全部为**合成轨迹/传感器数据**（解析式生成），不连接任何硬件
- **无 ROS 依赖**，无可视化/前端，无网络服务（JSON 经文件或标准输入/输出）

## 1. 功能

| 需求 | 实现 |
|---|---|
| 带时间序列的刚体变换树 | 每条边存 `T_parent_child` 的关键帧序列（或显式静态变换） |
| 任意两帧、任意时刻查询 | 最近公共祖先（LCA）寻路，上行取逆、下行连乘 |
| 拓扑环检测 | 建边时 DFS 判定新父节点是否位于子树内，成环即拒绝 |
| 多父冲突检测 | 每个子帧只允许一个父帧，挂第二个父帧即拒绝 |
| 时间覆盖 / 插值 | 仅在关键帧之间插值；越界一律报错，**绝不外推、绝不把最新值当历史值** |
| 时刻缺口检测 | 两侧关键帧间隔超过 `max_gap` 时抛 `TimeGapError` |
| 旋转插值 | 单位四元数 **slerp**（最短弧），平移线性插值 |
| JSON 入口 | `python -m transform_tree request.json`，也支持 stdin |

## 2. 目录结构

```
src/transform_tree/
  transform.py       # Transform：R,t 刚体变换、逆、复合、四元数、slerp
  timed_sequence.py  # 关键帧序列：严格覆盖插值、缺口检查、静态提供者
  tree.py            # TransformTree：建边、环/多父检测、LCA、时序查询
  json_io.py         # JSON 请求解析（全部输入做边界校验）与结果序列化
  main.py            # CLI：python -m transform_tree
  synthetic.py       # 合成轨迹与传感器数据（演示请求生成器）
  errors.py          # 全部异常类型
tests/               # pytest 自动化测试（99 项，见 RUN_REPORT.md）
scripts/handcalc_check.py  # 独立手算复核脚本（纸面公式 vs 库输出）
examples/
  request.json          # 完整演示：正常查询 + 缺口/越界/未知帧报错
  minimal_request.json  # 最小可读样例
  result.json           # 完整演示的实际运行结果
```

## 3. 安装与运行

无需安装，设置 `PYTHONPATH` 即可：

```bash
pip install -r requirements.txt          # 只需 numpy；pytest 仅测试用

# 用文件
PYTHONPATH=src python -m transform_tree examples/request.json

# 输出到文件
PYTHONPATH=src python -m transform_tree examples/request.json -o result.json

# 从标准输入
cat examples/minimal_request.json | PYTHONPATH=src python -m transform_tree -
```

退出码：`0` 全部查询成功；`1` 存在查询失败（错误逐条记录在结果中）；
`2` 请求本身无法解析或建树失败（环、多父、JSON 语法错等）。

## 4. 请求 / 响应格式

```json
{
  "default_max_gap": 0.6,
  "frames": ["base"],
  "edges": [
    {
      "parent": "base",
      "child": "link1",
      "type": "timed",
      "max_gap": 0.6,
      "keyframes": [
        {"t": 0.0, "translation": [0, 0, 0],
         "rotation": {"axis_angle": {"axis": [0, 0, 1], "angle": 0.0}}},
        {"t": 2.0, "translation": [0, 0, 1],
         "rotation": {"axis_angle": {"axis": [0, 0, 1], "angle": 1.5708}}}
      ]
    },
    {
      "parent": "link1", "child": "tool", "type": "static",
      "transform": {"translation": [0, 0, 0.15]}
    }
  ],
  "queries": [
    {"source": "tool", "target": "base", "time": 1.0, "max_gap": 0.6}
  ]
}
```

约定与字段：

- `T_parent_child` 表示 `p_parent = R · p_child + t`；查询返回
  `T_source_target`，即 `p_source = T_source_target · p_target`。
- `type`：`"timed"`（需非空、升序、无重复时间戳的 `keyframes`）或
  `"static"`（**必须显式声明**，静态边不受时间覆盖限制）。
- 旋转三选一：`{"quaternion": [w,x,y,z]}`、`{"matrix": [[3x3]]}`、
  `{"axis_angle": {"axis": [x,y,z], "angle": 弧度}}`；缺省为单位旋转。
  四元数会归一化，轴向量无需单位化；旋转矩阵必须正交且行列式 +1（拒绝反射）。
- 时间字段：`keyframes[].t` 与 `queries[].time` 均为秒（float）。
- `max_gap`：允许插值的相邻关键帧最大间隔；查询级 > 边级 > `default_max_gap`。
  间隔是**逐查询时刻两侧的两帧**判定的，不是全局采样率。

响应：

```json
{
  "ok": false,
  "frame_count": 5,
  "edge_count": 4,
  "results": [
    {
      "index": 0,
      "ok": true,
      "query": {"source": "tool", "target": "base", "time": 1.0},
      "chain": ["tool", "link1", "base"],
      "transform": {
        "translation": [...],
        "rotation_matrix": [[3x3]],
        "quaternion_wxyz": [w, x, y, z],
        "matrix4": [[4x4 齐次矩阵]]
      }
    },
    {
      "index": 3, "ok": false,
      "query": {"source": "tool", "target": "base", "time": 5.0},
      "error": {"type": "TimeGapError", "message": "..."}
    }
  ]
}
```

## 5. 时间语义（重点）

1. 查询时刻 `t` 必须落在所用每条动态边的 `[首帧, 末帧]` 闭区间内；
   早于首帧或晚于末帧都抛 `TimeNotCoveredError`。
   系统**不会**用末帧值顶替历史时刻，也不会向过去/未来外推。
2. `t` 恰为关键帧时间时直接取该帧；否则在相邻两帧间插值：
   平移线性，旋转四元数 slerp（`dot<0` 翻转走最短弧，小角度退化为归一化 LERP）。
3. 两侧关键帧间隔 > `max_gap`（数据断流）时抛 `TimeGapError`，
   不跨缺口猜测。
4. 一条路径上的每条边都在同一时刻独立求值；任一边越界/缺口，整条查询失败。

## 6. 库 API 速览

```python
from transform_tree import (
    Transform, TransformTree, Keyframe,
    TimedTransformSequence, StaticTransformProvider,
)

T_ab = Transform.from_quaternion([w, x, y, z], [tx, ty, tz])
seq  = TimedTransformSequence(
    [Keyframe(0.0, T0), Keyframe(2.0, T1)], max_gap=0.6)

tree = TransformTree()
tree.add_edge("base", "arm", seq)
tree.add_edge("arm", "tool", StaticTransformProvider(T2))

T = tree.lookup_transform("tool", "base", 1.0, max_gap=0.6)  # -> Transform
T.rotation, T.translation, T.inverse(), T @ other, T.transform_point(p)
tree.chain_frames("tool", "base")  # -> ["tool", "arm", "base"]
```

异常（均继承 `TransformTreeError`）：`CycleDetectedError`、
`MultipleParentsError`、`FramesNotConnectedError`、`UnknownFrameError`、
`TimeNotCoveredError`、`TimeGapError`、`DuplicateTimestampError`、
`InvalidTransformError`、`InvalidKeyframeError`、`InvalidRequestError`。

## 7. 手算验收（摘要）

模型：`T_base_arm = (Rz90°, t=(1,0,0))`，`T_arm_wrist = (Rz90°, t=(0,1,0))`。

- 复合 `T_base_wrist`：R = Rz180°，t = Rz90°·(0,1,0)+(1,0,0) = **(0,0,0)**；
  点 `(1,0,0)` → `(-1,0,0)`。
- 逆 `T_wrist_base`：R = Rz180°，t = (0,0,0)，正逆相乘 = I。
- 单段逆 `T_wrist_arm`：R = Rz(−90°)，t = **(−1,0,0)**。
- t=1 插值：两层各 Rz45°，复合 Rz90°，t =
  Rz45°·(0,0.5,0)+(0.5,0,0) = **(0.146447, 0.353553, 0)**。
- 时刻缺口与越界分别抛 `TimeGapError` / `TimeNotCoveredError`。

完整对比见 `scripts/handcalc_check.py`（实际运行记录见 `RUN_REPORT.md`）。

## 8. 测试

```bash
python3 -m pytest tests/ -v
python3 -m pytest tests/ --cov=transform_tree --cov-report=term-missing
PYTHONPATH=src python3 scripts/handcalc_check.py
```

## 9. 范围与非目标

- 仅离线计算：不接机器人、传感器、驱动，不读 bag/实时话题。
- 不提供任何前端页面、图形界面或可视化窗口。
- 不提供常驻网络服务；JSON 经文件/管道交互（可由上层再包 HTTP）。
- 数据为解析式合成，数值可复现（无随机种子依赖）。
