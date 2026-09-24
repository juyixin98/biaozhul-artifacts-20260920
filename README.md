# 二维双连杆机械臂逆运动学服务

基于 Python + FastAPI + NumPy/SciPy 的二维双连杆(2R 平面)机械臂正/逆运动学求解服务。
纯解析解, 只使用合成数据(FK 生成目标再送 IK 回代), 不接真实硬件, 不做可视化。

## 功能

- **正运动学 (FK)**: 关节角 `(theta1, theta2)` → 末端位置 `(x, y)`
- **解析逆运动学 (IK)**: 余弦定理求两支解 —— `elbow_down`(theta2 ≥ 0)与 `elbow_up`(theta2 ≤ 0)
- **关节限位**: 每支解单独判定是否在 `[min, max]` 闭区间内
- **状态如实上报, 不裁剪坐标伪装成功**:
  - `ok` — 至少一支解在限位内
  - `unreachable` — 目标在工作空间外(`d > L1+L2` 或 `d < |L1-L2|`), 返回空解
  - `limit_violation` — 几何可达但两支都越限, 仍附上解供诊断
  - `singular` — 完全伸直(theta2=0)或完全折叠(|theta2|=π), 两支退化为同一解
- **路径连续选解** (`/ik/path`): 沿目标点序列贪心选与上一步关节角最近的合法支,
  支持 seed 指定首点分支; 不可达点如实标记并断链, 不沿用旧关节角

## 目录结构

```
app/
  kinematics.py   # 运动学核心: FK/IK/限位/路径选解 (纯 Python + math)
  main.py         # FastAPI 接口层
tests/
  test_ik.py      # 25 个验收测试 (pytest)
examples/
  demo.py         # 验收示例: 通过 HTTP 覆盖全部验收场景
requirements.txt  # 锁定依赖 (pip freeze)
```

## 依赖与启动

需要 Python 3.12(开发验证环境为 3.12.3)。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动服务 (端口可自选)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8017

# 运行测试
.venv/bin/python -m pytest tests/ -v

# 运行验收示例 (需服务已启动, 端口与 demo.py 中 BASE 一致)
.venv/bin/python examples/demo.py
```

主要依赖: fastapi 0.141.1, uvicorn 0.53.0, numpy 2.5.3, scipy 1.18.1,
pytest 9.1.1, httpx 0.28.1(完整锁定见 requirements.txt)。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/fk` | `{theta1, theta2, arm?}` → `{x, y}` |
| POST | `/ik` | `{x, y, arm?}` → `{status, solutions[], reach_distance, singular}` |
| POST | `/ik/path` | `{points: [[x,y]...], seed?, arm?}` → 逐点 `{status, branch, theta1, theta2, step_jump}` |

`arm` 可选, 默认 `l1=l2=1.0`, 限位默认 `±π`。示例:

```bash
curl -s -X POST http://127.0.0.1:8017/ik \
  -H 'content-type: application/json' \
  -d '{"x": 1.0, "y": 1.0, "arm": {"l1": 1.0, "l2": 1.0,
       "limits": {"theta1_min": -3.14, "theta1_max": 3.14,
                  "theta2_min": -1.57, "theta2_max": 1.57}}}'
```

## 验收结果(实际运行记录)

测试: `25 passed`(pytest 9.1.1, Python 3.12.3)。覆盖:

- FK 已知位形正确性;随机 500 组关节角 FK→IK 关节空间还原
- 肘部翻转: 两支解 theta2 符号相反、均回代命中目标
- 完全伸直 / 完全折叠识别为 `singular`(两支退化为一支)
- 超程与内圈空洞(`d < |L1-L2|`)如实报 `unreachable`, 解为空
- 限位冲突(两支越限 → `limit_violation`)、单支合法、边界闭区间
- 路径连续选解: 50 点全程不翻肘, 相邻步关节跳变有界; seed 决定首支;
  中途不可达点如实标记、断链后恢复

验收示例 `examples/demo.py` 对运行中的服务实测(2026-09-24, 本机):

```
[PASS] FK->IK 回代末端误差 (200 随机样本)  max_err=2.310e-14
[PASS] 肘部翻转两支解  theta2_down=0.8000, theta2_up=-0.8000
[PASS] 完全伸直识别为 singular  status=singular, solutions=1
[PASS] 超程目标如实报 unreachable
[PASS] 限位冲突如实报 limit_violation  within_limits=[False, False]
[PASS] 路径连续选解 (50 点)  max_step_jump=0.0432 rad, branches={'elbow_down'}
```

## 已知限制 / 未完成项

- 仅二维双连杆(2 DOF), 不含末端姿态、不含 3D。
- 路径选解为贪心最近邻, 不做全局最优(如动态规划); 理论上存在贪心陷入
  局部分支的构型, 未实现回溯。
- 奇异判定用固定余弦容差(1e-9), 未做基于雅可比条件数的连续奇异度量。
- SciPy 已列入依赖备用于后续数值 IK(如阻尼最小二乘)扩展, 当前解析解
  路径未调用 SciPy。
- 无速度/加速度规划, 无碰撞检测。
