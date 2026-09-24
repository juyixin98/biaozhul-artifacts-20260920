# 二维双连杆机械臂逆运动学服务（FastAPI + NumPy + SciPy）

仅使用**合成数据 / 离线回放**（目标由正运动学生成或在工作区内参数化合成），
**不连接任何真实硬件，不做可视化**。

实现二维 2R 平面机械臂的：

- 正运动学（FK）与解析几何雅可比；
- **两支解析逆解**（elbow_up / elbow_down，闭式解，非迭代）；
- **关节限位**（按 2π 等价表示判定，支持跨越 ±π 的限位区间）；
- **沿路径连续选解**（相对上一可行点的加权关节角距离 + 同名滞回）；
- 明确区分 **完全伸直 / 完全折叠奇异、近奇异、不可达（外边界与内部空洞）、
  限位冲突**，失败时返回 `joints=null`——**绝不裁剪目标坐标伪装成功**。

---

## 1. 目录结构

```
app/
  kinematics.py   # FK / 雅可比 / 两支解析 IK / 限位 / 连续选解（核心）
  synthetic.py    # 合成数据与离线回放场景（全部目标来自 FK 或工作区内参数化）
  main.py         # FastAPI 路由 / 请求响应模型
examples/
  run_demo.py     # 离线验收示例（不启动服务也能跑）
tests/
  test_kinematics.py        # FK、雅可比（有限差分）、包裹、参数校验
  test_inverse.py           # 两支、翻转、伸直/折叠奇异、不可达、限位
  test_path.py              # 连续选解、奇异路径、不可达缺口、限位冲突
  test_scipy_crosscheck.py  # SciPy least_squares / SVD 独立交叉验证
  test_api.py               # HTTP 接口（FastAPI TestClient）
requirements.txt            # 运行时依赖（下限版本）
requirements-dev.txt        # 含 pytest 的开发依赖
requirements-lock.txt       # 实际验证通过的精确锁定版本（29 个包）
```

## 2. 依赖与启动

要求 Python 3.10+（实际验证环境为 Python 3.12.3 / Linux）。

```bash
# 1) 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装锁定依赖（推荐，复现验证环境）
pip install -r requirements-lock.txt
pip install pytest          # 锁文件已含 pytest；仅运行服务时可不装

#    或按下限版本安装：pip install -r requirements-dev.txt

# 3) 启动 HTTP 服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
#    浏览器打开交互式文档：http://127.0.0.1:8000/docs
```

运行测试：

```bash
pytest                       # 全部 69 个测试
```

运行离线验收示例（无需启动服务）：

```bash
python examples/run_demo.py
```

## 3. 坐标与肘部命名约定

- 基座（肩）固定原点；q1 为第一连杆相对 +x 轴角度，q2 为第二连杆相对角（弧度）。
- 末端：`x = l1 cos q1 + l2 cos(q1+q2)`，`y = l1 sin q1 + l2 sin(q1+q2)`。
- 工作区：r ∈ [ |l1−l2| , l1+l2 ]（等长臂内边界为 0，仅原点需折叠到达）。

肘部支名用几何叉积判定（避免不同教材 q2 正负号差异）：

```
cross = (W−O) × (E−O)    O=肩 W=肘 E=末端
cross < 0 → elbow_up （q2 = −α）
cross > 0 → elbow_down（q2 = +α）
```

奇异时两支退化为一支，支名为 `degenerate`。

## 4. 状态语义（不伪装成功）

| status             | 含义                                                        | joints      |
|--------------------|-------------------------------------------------------------|-------------|
| `ok`               | 可达、非奇异、满足限位                                      | 返回        |
| `near_singular`    | `|sin q2| < singular_eps`（默认 1e-6）但非严格奇异，雅可比病态 | 返回        |
| `full_extension`   | q2≈0 完全伸直（外边界 r=l1+l2），严格奇异                   | 返回退化支  |
| `full_fold`        | q2≈±π 完全折叠（等长臂时末端原点），严格奇异                | 返回退化支  |
| `unreachable`      | r>l1+l2 或（不等长臂）r<|l1−l2|；给 `boundary_gap`，不裁剪 | `null`      |
| `limit_violation`  | 两支都不满足关节限位（或显式偏好支撞限位）                  | `null`      |

严格奇异阈值为机器精度常量 `EXACT_SINGULAR_EPS=1e-11`（基于实际构型
`|sin q2|` 判定），与工作区边界容差 `reach_tol_ratio`（仅用于可达性与
`acos` 数值裁剪）解耦，保证 `near_singular` 区间不被边界容差带吞掉。

## 5. HTTP 接口

| 方法 路径                  | 说明                                                        |
|----------------------------|-------------------------------------------------------------|
| `GET  /`                   | 服务信息与路由列表                                          |
| `GET  /health`             | 健康检查                                                    |
| `POST /fk`                 | 正解：`{q1,q2,params?}` → 末端/肘部位置                     |
| `POST /ik`                 | 单点逆解：目标、两支明细、选中解、状态、误差、工作区信息     |
| `POST /ik/path`            | 目标序列连续选解 + 汇总（误差、翻转数、最大关节步长）       |
| `GET  /synthetic/acceptance` | 内置合成验收场景清单                                      |

`POST /ik` 请求示例：

```json
{
  "target": {"x": 1.0, "y": 1.0},
  "previous_joints": [0.0, 1.5707963267948966],
  "preference": null,
  "hysteresis": 1e-6,
  "w1": 1.0, "w2": 1.0,
  "params": {
    "l1": 1.0, "l2": 1.0,
    "theta1_min": -3.141592653589793, "theta1_max": 3.141592653589793,
    "theta2_min": -3.141592653589793, "theta2_max": 3.141592653589793
  }
}
```

连续选解规则：

- 在所有满足限位的解析分支中，选相对上一可行点
  `w1·|Δq1| + w2·|Δq2|`（角度按 2π 包裹）最小的一支；
- `hysteresis` 给与上一可行点**同名分支**减去一个小代价（默认 1e-6），
  两支代价接近时抑制抖动；设为 0 退化为纯最短角距离；
- 分支名由路径求解器显式记忆并用于翻转判定（跨越 ±π / 奇异后，
  仅凭关节角符号推断分支名不可靠）；
- 不可达 / 限位冲突点 `joints=null` 且**不更新**连续性基准，
  下一个可行点仍以最近一个可行点为参考。

curl 快速验证：

```bash
curl -s -X POST localhost:8000/ik -H 'Content-Type: application/json' \
  -d '{"target":{"x":1,"y":1},"preference":"elbow_up"}'
curl -s -X POST localhost:8000/ik -H 'Content-Type: application/json' \
  -d '{"target":{"x":3,"y":0}}'        # -> status=unreachable, joints=null
```

## 6. 验收结果（2026-09-24 实际运行）

环境：Python 3.12.3，Linux 6.8.0-90-generic；依赖精确版本见
`requirements-lock.txt`（fastapi 0.141.1、numpy 2.5.3、scipy 1.18.1、
uvicorn 0.53.0、pytest 9.1.1、httpx 0.28.1）。

### 自动化测试

```
pytest   →  69 passed, 0 failed（约 1.2s）
```

覆盖：FK 已知值与不等长臂；解析雅可比对 50 组随机位形的有限差分比对；
`det(J)=l1·l2·sin q2` 奇异行列式；**2000 个随机 FK 目标往返**（10 个种子
×200 点）；同目标两支解析解；显式偏好与连续性选支；完全伸直/折叠；
近奇异分类；外边界与内部空洞不可达；坐标不裁剪；限位单支/双支冲突、
显式偏好撞限位、跨 ±π 限位；路径连续选解、奇异路径、不可达缺口恢复；
以及用 **SciPy `least_squares` 数值逆解**与解析解的独立交叉验证、
**SciPy SVD** 对雅可比奇异值的秩亏验证；全部 HTTP 路由与 422 校验。

### 正解→逆解末端误差（`examples/run_demo.py` 实际输出）

- 两支解析解（目标 (1,1)）：肘上/肘下误差 2.22e-16 / 0.0；
- 完全伸直（q2=0, r=2）：`full_extension`，误差 0.0；
- 完全折叠（原点）：`full_fold`，误差 4.15e-16；
- 不可达 (2.4,0)/(2,2)/(内部空洞 r=0.2)：`unreachable`、`joints=None`、
  `boundary_gap` 分别为 0.4000 / 0.8284 / 0.2000；
- 限位冲突：偏好被挡支 → `limit_violation`；两支均挡 → `limit_violation`
  且分支明细透明返回；
- FK 平滑回放路径（61 点）：0 失败、0 翻转，最大末端误差 1.11e-15，
  最大关节步长 0.0500 rad；
- 合成圆路径（72 点，最近点 r=0.300）：0 失败、0 翻转，
  最大末端误差 7.71e-16；
- 圆路径粗采样（20 点，最近点同为 r=0.300，但相邻路径点更疏，
  更贴近完全折叠奇异）：仍 0 翻转，但最大关节步长放大到约 0.79 rad——这是近奇异构型下
  **病态雅可比导致的关节空间速度放大**（物理现象），不是选支抖动；
  选支始终保持同一分支；
- 不可达缺口路径：两个失败点返回 `null` 且不更新基准，恢复点关节角
  与缺口前完全一致（无跳变）；
- 2000 个随机 FK 目标：全部有解，最坏末端误差 2.74e-13。

真实 HTTP 服务（`uvicorn app.main:app`）已实际启动并用 curl 验证
`/health`、`/ik`（含伸直、不可达）、`/ik/path`（缺口）、422 校验、
`/docs` 与 `/synthetic/acceptance`，行为与离线结果一致。

## 7. 未完成项 / 已知边界

- **仅运动学**：无动力学、无碰撞检测、无时间参数化/速度规划；
  路径汇总只给几何与关节连续性指标。
- **无真实硬件接口、无可视化**（任务约束）；全部输入为合成/回放数据。
- 完全折叠（末端原点）时 q1 在物理上不可辨识，实现返回规范值 q1=0，
  并在 `note` 中注明。
- 连续选解为“按上一可行点局部最优”，不做全局分支规划；绕奇异路径
  是否换支取决于目标序列与权重（这是有意保留的行为，翻转数会显式上报）。
- 默认两支等权；如需抑制某关节运动可通过 `w1/w2` 调整，但未提供
  自动权重整定。
