# 离线 TF 时间缓存查询服务

带时间戳的 SE3 刚体变换树查询服务。**仅使用合成数据 / 离线回放，不接入任何
真实硬件，不做可视化。**

- 平移：时间线性插值
- 旋转：单位四元数 **SLERP**（正确处理 `q ≡ -q` 反号等价，始终走短弧）
- 逆变换：SE3 群逆（旋转转置 + 平移反号旋转）
- 多边组合：无向树 BFS 找链，逐边相乘
- 拒绝**成环**（添加会使两已连通帧再次相连的边 → 409）
- 拒绝**越界外推**（查询时间超出边的采样区间 → 400，端点吸附）

技术栈：Python 3.10+、FastAPI、NumPy、SciPy（`scipy.spatial.transform.Rotation`
负责四元数↔旋转矩阵转换，SLERP 为本项目手写实现）。

## 目录结构

```
.
├── pyproject.toml            # 包元数据 + pytest 配置
├── requirements.txt          # 锁定依赖（pip freeze 精确版本）
├── src/tf_cache/
│   ├── __init__.py
│   ├── errors.py             # 领域异常
│   ├── se3.py                # SE3Transform、SLERP
│   ├── tree.py               # TimeCache（单边）+ TransformTree（组合查询）
│   └── app.py                # FastAPI 路由与模型
├── tests/
│   ├── test_core.py          # 数学与树逻辑
│   └── test_api.py           # HTTP 接口（含异步并发采样）
└── examples/
    └── offline_replay.py     # 合成轨迹离线回放示例
```

## 环境准备与启动命令

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动 HTTP 服务（开发模式）
PYTHONPATH=src uvicorn tf_cache.app:app --reload
# 或：python -m uvicorn tf_cache.app:app --host 0.0.0.0 --port 8000
```

启动后：

- 交互式文档（Swagger UI）：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /health`

## 运行测试与示例

```bash
# 自动化测试
pytest

# 离线回放示例（不需要启动服务）
PYTHONPATH=src python examples/offline_replay.py
```

## HTTP API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| GET  | `/tree` | 列出全部坐标系、边、采样数与时间范围 |
| POST | `/transforms` | 写入一个带时间戳的边采样 |
| POST | `/transforms/batch` | 按边批量写入采样（须时间递增） |
| POST | `/lookup` | 查询单时刻组合变换 `T_target_source` |
| POST | `/lookup/batch` | 多时刻批量查询（`on_error: raise\|null`） |
| GET  | `/path/{source}/{target}` | 查询两坐标系的连通链 |
| DELETE | `/tree` | 清空内存中的树 |

四元数约定为 `[x, y, z, w]`（与 SciPy 一致）。

### 快速试用（curl）

```bash
curl -X POST localhost:8000/transforms -H 'Content-Type: application/json' -d '{
  "parent": "world", "child": "gripper", "t": 0.0,
  "translation": [1, 2, 3],
  "quaternion": [0, 0, 0.7071067811865475, 0.7071067811865476]
}'
curl -X POST localhost:8000/lookup -H 'Content-Type: application/json' -d '{
  "source": "gripper", "target": "world", "t": 0.0
}'
```

Python 库直接使用：

```python
from tf_cache import TransformTree, SE3Transform
from scipy.spatial.transform import Rotation

tree = TransformTree()
q = Rotation.from_euler("z", 90, degrees=True).as_quat()
tree.add_transform("world", "arm", 0.0, SE3Transform([0, 0, 0], q))
tree.add_transform("world", "arm", 1.0, SE3Transform([1, 0, 0], q))
tf = tree.lookup_transform("arm", "world", 0.5)  # 段内插值
```

## 语义约定

- `add_transform(parent, child, t, T)` 写入 `T_parent_child`，即
  `p_parent = T · p_child`。
- `lookup_transform(source, target, t)` 返回 `T_target_source`，即
  `p_target = T · p_source`。
- 按反方向重复写入同一条边时，写入值自动取逆。
- 时间戳单位为秒（浮点），采样必须严格递增；重复时间戳返回 409。
- 链上任一边在请求时刻无覆盖时，整体查询拒绝（不做外推）。

## 验收点对照

| 验收项 | 位置 |
|---|---|
| 90 度旋转 | `tests/test_core.py::Test90DegreeRotation`、`test_api.py::test_add_and_lookup_90deg` |
| 四元数反号 | `tests/test_core.py::TestQuaternionDoubleCover` |
| 异步采样 | `TestTimeHandling::test_async_concurrent_sampling` / `test_async_batch_under_asyncio`、`test_api.py::test_async_concurrent_batch_sampling` |
| 缺失链 | `TestTreeStructure::test_disconnected_chain`、`test_api.py::test_missing_frame_returns_404` |
| 组合后再逆变换误差 | `TestInverseAndComposition`、`test_api.py::test_compose_then_inverse_over_http`、回放脚本第 3 节 |

## 实测结果（2026-09-24，Python 3.12.3 / Linux x86_64）

锁定版本（见 `requirements.txt`，在全新 venv 中验证可复现安装）：
numpy 2.5.3、scipy 1.18.1、fastapi 0.141.1、pydantic 2.13.5、
uvicorn 0.53.0、pytest 9.1.1、httpx 0.28.1。

- `pytest`：**53 passed**（核心数学/树逻辑 + HTTP 接口；1 条来自 Starlette
  的 `httpx` 弃用告警，不影响功能，TestClient 行为正常）。
- `python examples/offline_replay.py`：成功。1001 个时刻扫描，
  组合后再逆变换的**最大平移残差 7.2e-15、旋转残差 6.3e-16**，点往返误差 2.7e-15；
  合成总旋转角 3.2e-15 deg（解析真值 0）；外推 / 缺失链 / 成环均被拒绝。
- uvicorn 实际启动并经 curl 验证：批量写入、单时刻查询（t=0.5 得 SLERP
  22.5° 旋转）、越界返回 400、成环返回 409，均符合预期。

## 已知限制 / 未做项

- 数据保存在单进程内存中，无持久化、无多副本（`DELETE /tree` 可清空）。
- 不做时间外推：超出采样区间的查询一律拒绝（这是验收要求，非缺陷）。
- 无鉴权 / 限流，定位为本地离线服务。
- 无可视化输出（按要求）。
- 回放脚本第 5 节 16×501 并发批量查询约 6 s（每次查询独立 BFS 找链 + 持锁），
  属正确性优先的朴素实现；如需高吞吐可加路径缓存。
