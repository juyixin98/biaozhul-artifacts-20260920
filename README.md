# edf — 最近障碍距离场（精确欧氏距离变换）

纯后端 Python 库：给定二维占用栅格与（可非方形的）栅格尺寸，离线计算每个
栅格到最近障碍物栅格的**精确欧氏距离**以及该最近障碍的**来源栅格坐标**。
只使用合成轨迹与传感器数据，不连接硬件、不做可视化、不依赖 ROS。

## 目录结构

```
edf/
  __init__.py          # 包入口：distance_field, brute_force_field
  distance_field.py    # 快速精确 EDT（Felzenszwalb–Huttenlocher 分离式变换）
  brute_force.py       # O(H·W·K) 暴力参考实现（验收基准）
  synthetic.py         # 合成世界 / 轨迹 / 激光扫描数据生成
  cli.py               # JSON 命令行入口
examples/
  request_sample.json  # 请求样例（由合成管线生成，24x18，cell 0.5 x 0.25 m）
tests/
  test_distance_field.py
  test_cli.py
requirements.txt
pytest.ini
```

## 安装

只需 Python ≥ 3.10 与 NumPy；测试需要 pytest：

```bash
pip install -r requirements.txt
```

## 库用法

```python
from edf import distance_field

occupied = [[False, False, False],
            [False, True,  False],
            [False, False, False]]
dist, sources = distance_field(occupied, cell_size=(0.5, 0.25))
# dist[y][x]    -> 到最近障碍的欧氏距离（米）；全空地图为 inf
# sources[y][x] -> 最近障碍栅格 (col, row)；全空地图为 None
```

`edf.brute_force_field` 是逐格扫描全部障碍的参考实现，接口相同，用于
小图校验。

## JSON 入口

```bash
python -m edf.cli examples/request_sample.json            # 结果到 stdout
python -m edf.cli examples/request_sample.json -o out.json
cat examples/request_sample.json | python -m edf.cli -    # stdin -> stdout
```

### 请求格式

```json
{
  "width": 24,
  "height": 18,
  "cell_size": [0.5, 0.25],
  "obstacles": [[0, 1], [0, 4]]
}
```

- `width` / `height`：正整数，栅格列数 / 行数。
- `cell_size`：可选，`[sx, sy]`（米），默认 `[1, 1]`；**支持非方形栅格**，
  必须为正且有限。
- 障碍二选一：`obstacles`（`[x, y]` 列表，`0 <= x < width`，
  `0 <= y < height`）或 `occupancy`（`height` 行 × `width` 列的真值数组，
  第 0 行即 y=0）。
- 未知字段被忽略。请求非法时退出码为 2，stderr 输出 `{"error": ...}`。

### 响应格式

```json
{
  "width": 24, "height": 18, "cell_size": [0.5, 0.25],
  "algorithm": "felzenszwalb-huttenlocher",
  "distances": [[0.25, 0.0, 0.5]],
  "sources": [[[0, 1], [1, 0], [1, 0]]]
}
```

- `distances[y][x]`：到最近障碍的距离（米）。**全空地图**无最近障碍，
  JSON 无法表示无穷大，统一编码为 `null`。
- `sources[y][x]`：最近障碍栅格 `[col, row]`；全空地图为 `null`。
- **全障碍地图**：每格距离为 `0.0`，来源为栅格自身。

## 合成数据

```bash
python -m edf.synthetic --seed 7 > request.json        # 直接生成 CLI 请求
python -m edf.synthetic --seed 7 --full                # 含轨迹与每帧扫描点
```

`synthetic.py` 生成带围墙与随机箱型障碍的合成世界，让虚拟机器人沿平滑
轨迹运动，用定步长射线投射模拟 2D 激光雷达，把命中点栅格化为障碍列表
——即 `examples/request_sample.json` 的来源。全程无硬件、无 ROS。

## 算法与精确性

- 核心为 Felzenszwalb–Huttenlocher 分离式平方距离变换：先沿列求每格所在
  列的最近障碍，再沿行做一维抛物线下包络变换。复杂度 O(H·W)。
- **不用曼哈顿/切比雪夫距离冒充欧氏距离**；输出为真实欧氏距离。
- 所有平方距离在**精确有理数**（`fractions.Fraction`）下计算：浮点栅格
  尺寸是二进有理数，因此 `((i-q)·sx)² + ((j-r)·sy)²` 全程无舍入，唯一的
  舍入发生在最终开方前转换为最近 `float` 的一刻。包络交点同样精确，
  仅在区间**可证明为空**时才弹出抛物线，单点相切的抛物线以零宽区间
  保留，供并列判定使用。
- **并列最近点规则**：距离精确相等的多个障碍中，取 `(row, col)` 最小者
  （行主序）。列内竖直并列取行号较小者；行变换中并列抛物线按标签比较。
  暴力参考实现使用同一规则，两者逐位一致。

## 测试与验收记录

自动化测试覆盖：小图暴力参考逐位比较（多随机种子、多栅格尺寸）、并列
最近点（双向/四角/三心共圆/对角 3-4-5/非方形栅格）、非方形栅格尺寸、
非方形地图、地图边界与四角障碍、全空地图、全障碍地图、单格地图、非法
输入、CLI 端到端（文件/stdin/occupancy 形式/错误退出码）、合成数据
确定性与一致性。

实际执行记录（本机 Python 3.12.3，NumPy 2.5.3，pytest 9.1.1）：

```bash
$ python3 -m pytest tests/ -q
77 passed in 3.53s
```

```bash
# 压力校验：1000 张随机图（尺寸 1..24，障碍率 0..0.9，
# cell_size ∈ {0.1,0.25,0.5,1.0,2.5}²），快速算法 vs 暴力参考逐位比较
trials=1000 fails=0
# 第二组（另一随机种子，尺寸 1..30，cell_size ∈ {0.05,0.1,0.3,1.0,1.5}²）
trials=300 fails=0
```

```bash
$ python3 -m edf.cli examples/request_sample.json -o /tmp/resp.json   # 退出码 0
$ echo '{"width":3,"height":2,"obstacles":[]}' | python3 -m edf.cli -
# -> distances/sources 全为 null（全空地图语义正确）
```

性能参考：200×200 随机图（10% 障碍）单次计算约 2.9 s（精确有理数运算
的代价；纯离线场景可接受）。

### 开发中发现并已修复的问题（如实记录）

初版实现用浮点数计算包络交点：当某抛物线仅在单个输出点与其它抛物线
精确并列时（典型情形：某格正上方与正左方各有一个障碍，cell 0.1 m），
浮点舍入会使其区间被误判为空而弹出，导致**距离正确但来源栅格与暴力
参考不一致**（首轮压力测试 300 例中 4 例失败，全部位于
cell_size=(0.1, 0.1)）。修复方式为全链路改用精确有理数语义（见上节），
修复后 1000 + 300 例压力测试 0 失败。当前无已知未通过项。

## 限制

- 为换取逐位可验证的精确性，内部使用有理数运算，速度低于纯浮点实现；
  超大地图（>10⁶ 格）会先感受到常数开销。
- 距离定义在栅格中心之间；未做亚栅格插值。
- 仅离线批处理，无增量更新、无 ROS 接口、无可视化。
