# 坐标变换时间树（transform-tree）

纯后端的机器人离线计算库：带时间序列的刚体变换树。使用 Python + NumPy 实现，
只消费合成轨迹/传感器数据，不连接硬件、不做可视化、不依赖 ROS。

## 功能

- 每个坐标系（除根外）恰有一条指向父坐标系的边，边上挂载 `(时间, 位姿)` 时间序列
- 查询任意两坐标系在**指定时刻**的相对变换（沿最近公共祖先路径组合）
- 平移线性插值、旋转球面线性插值（slerp）
- **严格时间覆盖**：查询时刻超出任一边的样本区间即抛 `ExtrapolationError`，
  绝不把最新（或最早）样本当作区间外时刻的值
- 拓扑校验：新增边时检测**环**（`CycleError`）与**多父冲突**（`MultiParentError`）
- JSON 请求/响应入口，可文件或标准输入驱动

## 约定

- 四元数分量顺序为 `(x, y, z, w)`，单位四元数，主动旋转约定
- 边上的变换 `parent_T_child` 表示"子坐标系在父坐标系中的位姿"：
  `p_parent = R @ p_child + t`
- `lookup_transform(target, source, time)` 返回 `target_T_source`，
  即把 source 系下的点映射到 target 系

## 安装与运行

```bash
pip install -r requirements.txt   # 仅 numpy（运行）与 pytest（测试）

# 运行测试
python3 -m pytest tests/ -v

# JSON 入口：文件或标准输入
python3 -m transform_tree examples/request_chain.json
cat examples/request_interpolation.json | python3 -m transform_tree
```

## JSON 接口

请求：

```json
{
  "transforms": [
    {"parent": "world", "child": "base", "time": 0.0,
     "translation": [1, 0, 0], "rotation_xyzw": [0, 0, 0, 1]}
  ],
  "queries": [
    {"target": "world", "source": "tool", "time": 5.0}
  ]
}
```

响应：`ok`、`frames`，以及按条返回的 `results`（成功时含
`translation` / `rotation_xyzw` / 4x4 `matrix`，失败时含
`error.type` / `error.message`）。加载 transforms 阶段的拓扑错误
（环、多父）使整个请求返回 `ok: false`；查询阶段的错误按条隔离。
进程退出码：整体失败为 1，否则为 0。

样例文件：

| 文件 | 内容 |
|------|------|
| `examples/request_chain.json` | 三层链 world→base→arm→tool，含正/逆查询与越界时刻 |
| `examples/request_interpolation.json` | 0°→90° 旋转边的中点插值与越界查询 |
| `examples/request_cycle.json` | 故意构造 a→b→c→a 环（整体失败演示） |

## 验收：手算三层变换链

合成数据（t∈[0,10] 上静态）：

- `world→base`：平移 (1,0,0)，无旋转
- `base→arm`：平移 (0,1,0)，绕 z 轴 +90°
- `arm→tool`：平移 (0,0,1)，无旋转

手算结果：

- `world_T_tool`：平移 **(1,1,1)**，旋转 **Rz(+90°)**；
  tool 系点 (1,0,0) 映射到 world 系 (1,2,1)
- `tool_T_world`（逆变换）：平移 **(-1,1,-1)**，旋转 **Rz(-90°)**；
  与正向组合为恒等
- 旋转插值：0°→90° 的边在中点时刻应得 **45°**（四元数 (0,0,sin22.5°,cos22.5°)）
- 时刻缺口：路径上任一边样本区间为 [0,4] 时，t=5 的整链查询必须失败

以上全部由 `tests/test_tree.py` 中的自动化测试断言。

## 实测记录

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1（Linux x86_64）。

```
$ python3 -m pytest tests/ -v
# 首轮：33 passed, 1 failed
#   FAILED tests/test_transform.py::test_compose_hand_computed
#   原因：测试注释中的手算式写错（把旋转误归到外层变换），库代码结果
#   (1,1,0) 经独立点映射验证是正确的；修正测试期望后复跑。
$ python3 -m pytest tests/ -q
34 passed in 0.47s

$ python3 -m pytest tests/ -q --cov=transform_tree --cov-report=term-missing
TOTAL  277 stmts, 86% cover, 34 passed
# __main__.py 显示 0% 是因为 CLI 测试在子进程中执行、覆盖率不跨进程统计，
# 其功能由 tests/test_json_api.py 的 subprocess 测试实际验证。

$ python3 -m transform_tree examples/request_chain.json
# ok=true；world_T_tool 平移 (1,1,1)、旋转 Rz90 ✓
#         tool_T_world 平移 (-1,1,-1)、旋转 Rz-90 ✓（与手算一致）
#         t=10.5 查询返回 ExtrapolationError「晚于最晚样本 10.0，禁止外推」✓

$ python3 -m transform_tree examples/request_interpolation.json
# t=5.0 中点：平移 (5,0,0)，四元数 (0,0,0.3827,0.9239) 即 45° ✓
# t=-1.0 返回 ExtrapolationError ✓

$ python3 -m transform_tree examples/request_cycle.json
# ok=false，CycleError「新增边 'c' -> 'a' 会形成拓扑环」，退出码 1 ✓
```

未通过项：首轮 `test_compose_hand_computed` 失败一次（测试手算式错误，
已修正并复跑通过）；除此之外无未通过项。

## 项目结构

```
transform_tree/
  __init__.py       # 包导出
  exceptions.py     # 异常类型（环/多父/外推/不连通/未知坐标系）
  quaternion.py     # 四元数运算与 slerp
  transform.py      # SE(3) 变换：组合、求逆、作用点、齐次矩阵
  timeseries.py     # 单条边的时间序列与带覆盖检查的插值
  tree.py           # 变换树：拓扑维护与指定时刻查询
  json_api.py       # JSON 请求/响应纯函数入口
  __main__.py       # CLI：python -m transform_tree [request.json]
tests/              # 34 项自动化测试
examples/           # JSON 请求样例
```

## 限制

- 纯离线批处理：每次请求独立建树，无增量更新与持久化
- 单根森林允许多棵互不相连的树，但跨树查询返回 `ConnectivityError`
- 插值为分段线性 + slerp，不提供样条等高阶插值
