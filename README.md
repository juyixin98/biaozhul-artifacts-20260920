# 六关节串联机械臂 — 数值逆运动学多解选择服务

纯后端数值逆解（IK）服务：Python + NumPy + FastAPI。使用**阻尼最小二乘（DLS）迭代**，从
**多个初值**（2×2×2 解析分支种子 + 结构化姿态 + 随机重启）出发，对每个收敛候选做一次
**独立的正运动学（FK）核验**，按候选与当前姿态的**角度周期距离**选最近分支。失败明确分为
三类：`unreachable`（不可达）、`singular_no_convergence`（奇异附近不收敛）、
`joint_limit_conflict`（限位冲突）。

**本服务永远不会返回未经 FK 核验通过的成功结果**：核验是独立于迭代残差的第二道关卡，
HTTP 层另有一道硬闸门；测试还会故意破坏核验器，确认此时不会报告成功。

---

## 1. 机器人模型与 DH 参数

6 个转动关节，单位：米 / 弧度。采用 Craig 改进 DH 约定

```
^{i-1}T_i = Rx(α_{i-1}) · Tx(a_{i-1}) · Rz(θ_i) · Tz(d_i)
```

| 关节 i | a_{i-1} (m) | α_{i-1} (rad) | d_i (m) |
|------:|------------:|--------------:|--------:|
| 1 | 0.000 |  0       | 0.150 |
| 2 | 0.000 | −π/2     | 0.140 |
| 3 | 0.250 |  0       | 0.000 |
| 4 | 0.020 | −π/2     | 0.250 |
| 5 | 0.000 |  π/2     | 0.000 |
| 6 | 0.000 | −π/2     | 0.085 |

关节软限位（服务返回值必在限位内）：

| 关节 | 下限 | 上限 |
|---|---:|---:|
| q1 | −160° | 160° |
| q2 | −110° | 110° |
| q3 | −135° | 135° |
| q4 | −266° | 266° |
| q5 | −100° | 100° |
| q6 | −266° | 266° |

数值标定的工作空间（对 q2/q3 密集采样腕心）：腕心最大半径 ≈ 0.641 m，
TCP（d6=0.085 m）可达包络半径 ≈ 0.720 m。定义在 `app/robot.py` 顶部，可直接核对。

---

## 2. 算法要点（`app/robot.py`）

1. **正运动学 `fk`**：按 MDH 逐帧累乘 4×4 齐次矩阵，返回 R、p；同时给出每帧
   位姿用于雅可比。
2. **几何雅可比 `geometric_jacobian`**：注意 Craig MDH 下关节 i 的转轴是
   `Rx(α)Tx(a)` 之后中间帧的 z 轴、过 `Tx(a)` 后的点（不是 frame {i} 的原点）；
   该实现已用中心差分对随机姿态逐一验证。
3. **阻尼迭代 `_dls_run`**：误差为基坐标系下 `[角度轴(3); 位置(3)]`，姿态误差用
   `R_err=R_des·R_curᵀ` 的测地角（对 180° 有稳健处理）。迭代式
   `Δq = Jᵀ(JJᵀ + λ²I)⁻¹ e`；λ 随最小奇异值自适应增大（奇异时 0.01→0.05），
   单步限幅 0.3 rad，每步夹紧限位并统计夹紧比例。
4. **多初值**（三层）：
   - 解析分支种子：肩左右 × 肘上下 × 腕翻转，最多 8 个（闭式几何对本臂精确，
     迭代只做数值抛光），保证选解时看得到所有分支；
   - 结构化姿态（home、四周典型姿态、当前姿态）；
   - 仅当前两层无核验通过候选时，才用确定性随机重启（可复现，seed 可传）。
5. **独立 FK 核验 `verify_solution`**：对每个收敛候选重新跑一遍 FK，位置误差
   ≤ 2e-4 m、姿态误差 ≤ 1e-3 rad 且在限位内才算通过。迭代残差本身从不作为成功依据。
6. **多解选择**：在全部核验通过的候选中，最小化到 `current_joints` 的
   **周期距离** `wrap_to_pi(q_candidate − q_current)` 的加权 RMS（默认权重全 1，
   可用 `weights` 加重某关节）。距离相同时取残差更小者。
7. **失败分类**：粗包络预拦截 → `unreachable`；迭代停滞且最小奇异值塌陷、误差停在
   奇异方向 → `singular_no_convergence`；最近候选被限位反复夹紧
   （夹紧比例 ≥ 0.15 且残差已很小）→ `joint_limit_conflict`；其余仍不收敛 →
   `unreachable`。

**角度周期距离**：`wrap_to_pi / periodic_distance` 把角度差折回 (−π, π]。
例如 π 与 −π 的周期距离为 0（普通差值会得到 2π），2π 与 0 的周期距离也是 0。
选择分支、去重初值都使用该距离，不使用普通差值。

---

## 3. 接口协议

基地址 `http://127.0.0.1:8000`。

### `GET /health`
无需签名，返回状态与可达半径。

### `POST /api/v1/ik/solve` （需 HMAC 签名）

请求体：

```json
{
  "position": [0.45, 0.0, 0.10],
  "rotation": [[1,0,0],[0,1,0],[0,0,1]],
  "current_joints": [0, 0, 0, 0, 0, 0],
  "weights": [1, 1, 1, 1, 1, 1],
  "seed": 20260923
}
```

- `position`：基坐标系下 TCP 目标，米，长度 3。
- `rotation`：3×3 旋转矩阵 `R_{0,6}`，必须正交且 det=+1（反射矩阵返回 422）。
- `current_joints`：可选，当前关节角（弧度）；用于选最近分支。
- `weights`：可选，6 个正数。
- `seed`：可选，随机重启种子，默认 20260923。

成功响应（节选）：

```json
{
  "status": "ok",
  "joint_angles_rad": [ ... 6 个弧度 ... ],
  "joint_angles_deg": [ ... 6 个角度 ... ],
  "verification": {
    "position_error_m": 1.2e-16,
    "orientation_error_rad": 0.0,
    "within_limits": true,
    "passed": true
  },
  "candidates_evaluated": [ { "joint_angles_deg": [...],
    "periodic_distance_to_current_rad": 0.0, "...": "..." } ],
  "selection": { "criterion": "minimum periodic weighted distance ..." },
  "num_initial_guesses": 8
}
```

失败响应 HTTP 200 + 机器可读 `status`（`unreachable` /
`singular_no_convergence` / `joint_limit_conflict`）与 `reason`，且不返回关节角。

### 请求签名（真实 HMAC-SHA256，非占位）

规范串：

```
<METHOD>\n<PATH>\n<X-Timestamp>\n<sha256_hex(原始请求体)>
```

请求头：

| 头 | 含义 |
|---|---|
| `X-Key` | 密钥 ID |
| `X-Timestamp` | Unix 秒；与服务器时差超过 ±300 s 拒绝（防重放） |
| `X-Signature` | `hex(HMAC_SHA256(secret, 规范串))` |

服务端用标准库 `hmac.compare_digest` 做恒定时间比较。密钥由环境变量
`IK_API_KEYS="id1=secret1,id2=secret2"` 配置；**默认开发密钥
`demo-key-id / demo-secret` 仅供本地验收，部署必须替换**。时差窗口可用
`IK_TIMESTAMP_SKEW` 调整。

---

## 4. 本地启动

```bash
cd P064/a
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 锁定依赖，可复现
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

可选：自定义密钥启动

```bash
IK_API_KEYS="arm-ops=$(openssl rand -hex 24)" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## 5. 验收命令

### 5.1 自动化测试（46 项）

```bash
.venv/bin/python -m pytest -q
```

覆盖：FK 与独立 4×4 参考实现一致、雅可比与中心差分一致、20 组“已知正解生成的目标”
往返并由测试第三条代码路径再次 FK 核验、周期距离语义（π/−π、2π/0）、参考姿态不同则
选不同分支、权重翻转分支、纯伸直奇异位姿精确可达且核验通过、奇异方向外推 5 mm
不收敛、超范围不可达、181° 方位限位冲突、三类失败互不混淆、4 组不同初值种子均核验、
“核验器被破坏时绝不返回成功”、以及 HTTP 层的有效签名/缺头/错签名/未知密钥/过期时间戳/
请求体篡改/det=−1 矩阵等用例。

### 5.2 端到端示例（真实 HTTP + 真实 HMAC）

生成示例（已随仓库提供，可重新生成）：

```bash
.venv/bin/python scripts/gen_examples.py
```

服务启动后另开终端：

```bash
.venv/bin/python scripts/ik_client.py examples/01_known_fk_target.json        # -> ok
.venv/bin/python scripts/ik_client.py examples/02_straight_singular_exact.json # -> ok（伸直奇异精确位姿）
.venv/bin/python scripts/ik_client.py examples/03_near_singular_no_convergence.json
                                                                              # -> singular_no_convergence
.venv/bin/python scripts/ik_client.py examples/04_out_of_reach.json           # -> unreachable
.venv/bin/python scripts/ik_client.py examples/05_joint_limit_conflict.json   # -> joint_limit_conflict
curl -s http://127.0.0.1:8000/health
```

无签名 / 错签名 / 过期时间戳一律 `401`；客户端 `scripts/ik_client.py` 自身也会拒绝
`verification.passed != true` 的“成功”。

### 5.3 期望结果一览

| 示例 | 构造方式 | 期望 status |
|---|---|---|
| 01 | 随机限位内关节角做 FK 生成目标（多分支） | `ok` |
| 02 | q2=0,q3=−π/2 的纯伸直奇异精确位姿 | `ok`（精确目标仍核验） |
| 03 | 该奇异位姿沿径向向外推 5 mm（仍在粗包络内） | `singular_no_convergence` |
| 04 | 目标半径 3 m，远超 0.72 m | `unreachable` |
| 05 | 基座方位 181°，q1 所有分支都被迫越过 ±160° | `joint_limit_conflict` |

---

## 6. 目录结构

```
app/
  robot.py     # DH 参数、限位、FK、雅可比、DLS、多初值、核验、分类
  schemas.py   # 请求/响应模型与旋转矩阵校验
  auth.py      # HMAC-SHA256 签名依赖与防重放
  main.py      # FastAPI 路由 + “无核验不成功”硬闸门
scripts/
  gen_examples.py  # 生成 examples/*.json
  ik_client.py     # 真实签名参考客户端
tests/
  test_ik.py       # 46 项验收测试
examples/          # 5 类场景的可签名请求体
requirements.txt       # 直接依赖（精确版本）
requirements-lock.txt  # 全量传递锁定
```

## 7. 设计取舍与边界

- 解析种子对本臂几何精确，因此常见目标单层即可成功且分支完整；结构化/随机重启只在
  解析集无核验候选时付出成本，兼顾“多初值”健壮性与延迟。
- 粗可达性检查只针对目标位置的轴对称包络，带 2 cm 裕量；腕姿态导致的真实不可达交给
  迭代+分类器，避免预检查误杀。
- 容差（2e-4 m / 1e-3 rad）、阻尼、奇异阈值、限位夹紧比例均为模块常量，集中在
  `app/robot.py` 顶部，便于按真机标定调整。
