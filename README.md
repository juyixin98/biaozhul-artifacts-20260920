# 坐标变换时间缓存（Offline TF Tree Query Service）

离线 TF 树查询服务：存储带时间戳的 SE(3) 变换，支持按时间插值查询、
逆变换与多边链式组合。仅使用合成数据 / 离线回放，不连接真实硬件，不做可视化。

## 功能

- 每条边（`parent -> child`）存储一个按时间排序的 SE(3) 样本序列；
- 查询时**平移线性插值、旋转 SLERP**（基于 `scipy.spatial.transform.Slerp`）；
- 四元数反号（`q` 与 `-q`）自动按短弧插值，不会产生多余的 180° 翻转；
- 支持**逆变换**（沿边反向遍历）与**多边组合**（BFS 找链后逐边组合）；
- **拒绝环**：自环、成环、给一个坐标系加第二个父节点都会报 `CycleError`；
- **拒绝越界外推**：查询时间超出边的样本范围时报 `ExtrapolationError`；
- 单样本边视为静态变换（任意时间有效，类似 ROS 的 `/tf_static`）。

## 坐标约定

- 边 `parent -> child` 存储 `T_parent_child`（child 坐标系在 parent 下的位姿）；
- `lookup(source, target, time)` 返回 `T_target_source`，即把 source 坐标系下的
  点变换到 target 坐标系：`p_target = T * p_source`；
- 四元数顺序为 `[x, y, z, w]`（与 SciPy 一致），时间单位为秒（浮点）。

## 环境与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖：Python 3.12，numpy / scipy / fastapi / uvicorn（版本见 `requirements.txt`，已锁定）。

启动 HTTP 服务：

```bash
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
```

运行测试：

```bash
.venv/bin/python -m pytest tests/ -v
```

运行离线回放示例：

```bash
.venv/bin/python examples/replay_demo.py
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/frames` | 列出所有坐标系与边 |
| POST | `/transforms` | 写入一条样本：`{parent, child, time, translation:[x,y,z], rotation:[x,y,z,w]}` |
| GET | `/lookup?source=&target=&time=` | 查询 `T_target_source`，返回 `{translation, rotation}` |

错误码：`404` 链不存在 / 无数据，`409` 成环，`416` 越界外推，`422` 参数非法。

示例：

```bash
curl -X POST localhost:8000/transforms -H 'Content-Type: application/json' -d \
  '{"parent":"world","child":"base","time":0.0,"translation":[1,0,0],"rotation":[0,0,0.70710678,0.70710678]}'
curl "localhost:8000/lookup?source=base&target=world&time=0.0"
```

## 项目结构

```
tf_cache/
  transform.py   # SE3：组合、求逆、插值（lerp + SLERP）
  buffer.py      # 单条边的时间序列缓存与插值查询
  tree.py        # TF 树：环检测、链查找、多边组合
  exceptions.py  # 异常体系
app.py           # FastAPI HTTP 层
tests/           # pytest 自动化测试
examples/replay_demo.py  # 合成数据离线回放示例
```

## 实测结果（2026-09-24，Python 3.12.3）

- `pytest tests/ -v`：**30 passed**（首轮 28/30，`test_cycle_rejected` 与对应 API 用例失败——
  根因是环检测只查了重复父节点、漏了祖先链，修复 `tree.py` 后全部通过）；
- `python examples/replay_demo.py`：42 个合成样本回放成功，t=1.0 查询结果
  `[1.0, 0.0, 0.5]` / 绕 z 轴 45°，与真值一致；组合后再逆变换的往返误差
  平移 1.0e-17 m、旋转 0 rad；t=99 越界查询被正确拒绝；
- uvicorn 实机冒烟：`/health`、`/transforms`、`/lookup`（t=1.0 插值结果
  平移 `[2,0,0]`、旋转 135°，介于 90° 与 180° 之间，正确）、`/frames` 正常；
  越界返回 416、缺失链返回 404。

## 已知限制 / 未完成项

- 数据保存在内存中，进程退出即丢失，没有持久化；
- 没有时间窗口淘汰（buffer 无限增长），长时间回放需自行清理；
- 单样本边按静态变换处理，若需要“单样本也拒绝外推”的语义需改 `buffer.py`；
- 未做并发写加锁（单进程内 FastAPI 默认线程模型下插入与查询未加互斥）；
- 无可视化（按要求）。
