# 六轴串联机械臂 · 数值逆运动学多解选择服务（纯后端）

Python + NumPy + FastAPI 实现的 6R 串联机械臂数值逆解 HTTP 服务。
核心特性：

- **阻尼最小二乘（DLS / Levenberg–Marquardt 风格）**数值迭代，步长自适应、可穿过腕部奇异；
- **多初值**：当前姿态、零位、限位中点、2 连杆**解析腕部初值**（肘上/肘下 × 方位两支 × 4 种腕构型）+ 分散构型 + 用户额外初值；
- **每个候选都用独立正运动学重新核验**（位置 + 轴角姿态误差），**未通过核验绝不返回成功**；
- **多解择优**：所有核验候选按与当前姿态的**周期关节距离**排序选解，周期距离并列时保留当前所在周期支（避免 J6 无谓整圈回转）；
- **角度周期距离**：`wrap_to_pi(a-b)`，绝不使用普通差值；
- **三类失败严格分开**：`UNREACHABLE`（超工作空间）/ `SINGULAR_NO_CONVERGE`（可达但阻尼迭代在奇异附近不收敛）/ `LIMIT_CONFLICT`（无限位解存在但无法落入关节限位，已枚举 2π 等价角与球形腕翻腕分支）；
- **真实密码学鉴权**：HMAC-SHA256 请求签名（绑定请求体 SHA-256）+ 时间戳/nonce 防重放 + 恒定时间比较；未配置密钥且开启鉴权时**拒绝启动**。

---

## 1. 目录结构

```
app/
  robot_params.yaml      # DH 参数、关节限位、求解器容差（项目内提供）
  params_loader.py       # YAML 加载（PyYAML，缺失时回退内置最小解析器）
  config.py              # 求解配置 + 密码学环境变量配置
  robot_model.py（core/） # 标准 DH 正运动学、中心差分雅可比、轴角姿态误差
  angles.py              # wrap_to_pi / 周期距离 / 限位周期代表角
  reachability.py        # 球形腕 2 连杆解析可达性预检（区分超范围/限位）
  ik.py                  # DLS 迭代、多初值、两阶段求解、核验、多解选择、四分类
  schemas.py             # HTTP 请求/响应 Pydantic 协议（含旋转矩阵正交校验）
  security.py            # HMAC-SHA256 签名校验、防重放 nonce 缓存
  main.py                # FastAPI 应用与路由
tests/                   # pytest：角度/FK/可达性/IK 全分类/HTTP/HMAC 共 40 例
examples/                # 5 个示例请求（均由真实 FK 生成）
scripts/
  sign_request.py        # 签名并发送请求的命令行客户端
  acceptance_offline.py  # 离线验收（8 类场景）
requirements.txt         # 锁定依赖（实测版本）
```

## 2. 机械臂参数（标准 DH）

| 关节 | a (m) | d (m) | α (rad) | θ_offset |
|---|---|---|---|---|
| 1 | 0.00 | 0.65 | +π/2 | 0 |
| 2 | 0.35 | 0.00 | 0 | 0 |
| 3 | 0.35 | 0.00 | +π/2 | 0 |
| 4 | 0.00 | 0.35 | −π/2 | 0 |
| 5 | 0.00 | 0.00 | +π/2 | 0 |
| 6 | 0.00 | 0.12 | 0 | 0 |

- 球形腕（关节 4/5/6 轴交于腕点）；**零位为完全伸直的最大臂展位姿**，
  肩轴→腕点最大距离 `a2 + sqrt(a3²+d4²) = 0.8450 m`（伸直奇异在 q3 = atan2(d4,a3) = π/4）。
- 关节软限位：J1 ±170°，J2 [−100°, +45°]，J3 ±135°，J4 ±180°，J5 ±100°，J6 ±360°。
- 完整参数与求解容差见 `app/robot_params.yaml`，运行时也可经
  `GET /api/v1/robot/info` 获取与计算完全一致的同一份配置。

## 3. 本地启动

需要 Python 3.11+（实测 3.12）。

```bash
cd /path/to/project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 方式 A（推荐，默认开启 HMAC 鉴权）
export IK_API_KEY='please-change-me-to-a-long-random-secret'
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 方式 B（仅本地调试，关闭鉴权）
IK_REQUIRE_AUTH=false uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：

- `GET /healthz` 健康检查（无需鉴权）
- `GET /api/v1/robot/info` DH 参数/限位/求解器配置
- `POST /api/v1/ik` 逆运动学（需鉴权）
- 交互式文档：`http://127.0.0.1:8000/docs`

> 注意：`IK_REQUIRE_AUTH=true`（默认）但未设置 `IK_API_KEY` 时服务**直接报错退出**，
> 不会以空密钥静默放行。

## 4. 请求 / 响应协议

请求（角度全部为弧度）：

```json
{
  "position": [0.5, 0.0, 0.5],
  "orientation": { "rotation_matrix": [[1,0,0],[0,1,0],[0,0,1]] },
  "current_joints": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
  "extra_seeds": [[0.3,-0.5,0.9,0.4,0.5,0.2]]
}
```

- `orientation` 二选一：`rotation_matrix`（3×3，做正交性与行列式校验）或
  `quaternion`（`[x,y,z,w]`，自动归一化）；
- `current_joints` 缺省以零位作为择优参考；
- `extra_seeds` 可选，用于客户端利用上周期解等先验。

响应：

```json
{
  "status": "SUCCESS",
  "success": true,
  "message": "38 个候选通过正解核验，已按离当前姿态的周期关节距离择优",
  "joints": [0.5999, -0.4000, 0.8000, 0.5000, 0.3000, 0.9000],
  "joints_wrapped": [0.5999, -0.4000, 0.8000, 0.5000, 0.3000, 0.9000],
  "position_error": 0.0000012,
  "orientation_error": 0.0000004,
  "candidates": [ { "joints": [...], "joint_distance_to_current": 0.0,
                    "raw_distance_to_current": 0.0, "...": "..." } ],
  "diagnostics": { "seed_count": 31, "unconstrained_solutions": 13,
                    "smallest_singular_value": 0.0 }
}
```

失败时 `success=false`、`joints=null`，`status` 为
`UNREACHABLE` / `SINGULAR_NO_CONVERGE` / `LIMIT_CONFLICT`，并在 `diagnostics` 与 `message` 中给出证据
（最大臂展/请求腕距、最小雅可比奇异值、越限关节与每组无约束解的限位情况）。

### 4.1 HMAC-SHA256 签名

共享密钥经 `IK_API_KEY` 配置。请求头：

| 头 | 内容 |
|---|---|
| `X-IK-Key-Id` | `default` |
| `X-IK-Timestamp` | Unix 秒 |
| `X-IK-Nonce` | 随机串（时间窗内不可重复） |
| `X-IK-Signature` | `hex(HMAC_SHA256(key, f"{key_id}\n{ts}\n{nonce}\n{sha256(body)}"))` |

时间戳容差默认 ±300 s（`IK_TIMESTAMP_TOLERANCE`）。直接用随附客户端：

```bash
export IK_API_KEY='please-change-me-to-a-long-random-secret'
python3 scripts/sign_request.py examples/01_reachable_target.json
```

## 5. 验收命令

```bash
# 1) 自动化测试（40 例：计算正确性 + 协议 + 密码学）
IK_REQUIRE_AUTH=false python3 -m pytest -q

# 2) 离线场景验收（已知正解/伸直奇异/超范围/限位冲突/初值变化/周期选解/三分类）
python3 scripts/acceptance_offline.py

# 3) 启动真实服务并端到端调用
export IK_API_KEY='please-change-me-to-a-long-random-secret'
uvicorn app.main:app --port 8000 &
python3 scripts/sign_request.py examples/01_reachable_target.json
python3 scripts/sign_request.py examples/02_periodic_joint6.json
python3 scripts/sign_request.py examples/03_unreachable.json
python3 scripts/sign_request.py examples/04_limit_conflict.json
python3 scripts/sign_request.py examples/05_quaternion_with_seeds.json
```

## 6. 关键实现说明

### 6.1 为什么有两阶段求解
阶段 A 用**无限位约束**的多初值 DLS 找出目标的真实解（判断“到底能不能到”）；
阶段 B 把每个核验解展开为球形腕翻腕等价支（`(q4,q5,q6)` 的 4 组等价），
枚举各轴在限位区间内的**全部 2π 周期代表角**，以其为热启动跑**带限位**的 DLS。
这样三类失败可严格区分：

- 阶段 A 无解 + 解析预检超臂展 → `UNREACHABLE`；
- 阶段 A 无解 + 预检可达 → `SINGULAR_NO_CONVERGE`；
- 阶段 A 有解但阶段 B 全部无法在限位内核验 → `LIMIT_CONFLICT`。

### 6.2 周期距离与限位代表
关节角是周期量：`dist(a,b) = |wrap_to_pi(a-b)|`，
`dist(5.5, -0.78) ≈ 0` 而普通差为 6.28。多解择优用周期距离作为运动代价；
但同一角的两个 2π 代表（q6=+5.52 与 −0.76）周期距离**恒等**，
因此并列时用**原始数值距离**挑出与当前姿态同周期支的代表，避免整圈回转。

### 6.3 核验
候选成功判定不是“迭代收敛了”，而是：
`fk(candidate)` 相对目标的位置/姿态误差 ≤ 核验容差（默认 1e-4 m / 1e-4 rad）
且候选逐轴落在限位内。测试 `test_no_success_without_fk_verification`
把核验容差收紧到 1e-14，断言服务不再返回任何 SUCCESS。

### 6.4 失败如实报告
- `UNREACHABLE` 与 `SINGULAR_NO_CONVERGE` 的区分由解析预检（球形腕 2 连杆闭式解）
  与多初值数值结果**交叉验证**，二者不一致时按“无解原因”保守归类并在 `diagnostics` 同时给出；
- 单解耗时约 0.3–1.0 s（约 30 组初值 × 两次阶段），纯 NumPy 同步实现；
  生产可加进程池，当前规模无需。
