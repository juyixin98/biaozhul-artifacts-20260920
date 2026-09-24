# 标定链传播审计服务（Calibration Chain Propagation Audit）

纯后端服务：输入各坐标变换、6 维小扰动协方差与标定版本，沿坐标帧链
**统一扰动约定**传播协方差，并检查闭环误差。对非法旋转、非正定协方差和
闭环冲突给出定位到具体边/回路的**最短证据路径**；未知相关性绝不默认
"严格独立且不说明"。另提供 Monte Carlo 合成样本，用精确 SE(3) 群运算核对
一阶传播近似，覆盖小角度、长链与缺失协方差三类情形。

技术栈：**Python 3.12 · NumPy · FastAPI**，密码操作为真实 Ed25519 + SHA-256。

---

## 1. 快速开始

```bash
# 1) 创建虚拟环境并安装锁定依赖
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock

# 2) 启动服务（首次启动会在 keys/ 下真实生成 Ed25519 开发密钥）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3) 健康检查（另开终端）
curl -s http://127.0.0.1:8000/health | python -m json.tool
```

交互式 OpenAPI 文档：<http://127.0.0.1:8000/docs>

### 验收命令（一次性）

```bash
# 全量自动化测试（53 个用例，含真实 Monte Carlo，约 90 秒）
. .venv/bin/activate
pytest -q

# 三个代表性审计：正常闭环 / 闭环冲突 / 非法输入
curl -s -X POST http://127.0.0.1:8000/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_basic.json | python -m json.tool
curl -s -X POST http://127.0.0.1:8000/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_conflict.json | python -m json.tool
curl -s -X POST http://127.0.0.1:8000/audit \
  -H 'Content-Type: application/json' \
  -d @examples/audit_illegal.json | python -m json.tool

# Monte Carlo 核对（长链，小扰动）
curl -s -X POST http://127.0.0.1:8000/montecarlo \
  -H 'Content-Type: application/json' \
  -d @examples/montecarlo_long_chain.json | python -m json.tool
```

密钥工具（真实密钥，非占位）：

```bash
python scripts/keygen.py keys/my_key          # 生成 Ed25519 密钥对
python scripts/sign_payload.py keys/my_key.pem examples/audit_basic.json bundle.json
```

---

## 2. 数据约定（务必先读）

### 2.1 边（变换）语义

每条边 `TransformSpec` 给出一个刚体变换 `T`（child → parent）：

* `parent_frame` / `child_frame`：`T` 把 **child 坐标系下的点映射到 parent
  坐标系**，即 child 在 parent 中的位姿。
* `translation`：`t ∈ ℝ³`。
* `rotation`：单位四元数 `[w,x,y,z]`（Hamilton）或 3×3 旋转矩阵；二者都做
  合法性校验（单位模、正交、det=+1）。
* `covariance`：**6×6** 小扰动协方差，切向量顺序
  `[ρx, ρy, ρz, φx, φy, φz]`（先平移后轴角旋转）。置 `null`/缺省表示
  **未知**，系统不会把它当零（见 §4）。

### 2.2 扰动约定（请求级统一）

`convention` 对**所有边**强制统一，不允许逐边覆盖（混入另一种约定的边会被
拒绝并报 `mixed_convention`）：

* `"right"`（默认）：`T = T̄ · Exp(ξ)`，扰动在**体坐标系（body）**。
* `"left"`：`T = Exp(ξ) · T̄`，扰动在**空间坐标系（spatial）**。

沿帧路径 `[F0, F1, …, Fn]`，第 k 步矩阵 `Xk` 把 Fk 坐标映射到 F{k+1}。
行走乘积（后施加的变换在最左）为

```
W = X_{n-1} … X_1 X_0
```

一步的 6 维扰动 `ξk` 到总残差切空间的一阶系数（伴随矩阵 `Ad`）：

* 右扰动，body 增量 `log(W′·W⁻¹)`：`Jk = Ad(Hk)`，`Hk = X_{n-1}…Xk`；
* 左扰动，spatial 增量 `log(W⁻¹·W′)`：`Jk = Ad(Pk⁻¹)`，`Pk = Xk…X0`。

这两个系数均已用有限差分在 1e-10 量级验证（`tests/test_se3.py`）。
反向经过一条边时，其局部协方差按约定映射：右扰动乘子 `Ad(T)`，左扰动
乘子 `Ad(T⁻¹)`。

协方差传播：`Σ = Σk Jk Sk Jkᵀ`，外加相关性策略给出的交叉项（见 §4）。

### 2.3 闭环残差

闭环行走 `[…, F0]` 的残差为 `ξ = vee(log(W_loop)) ∈ ℝ⁶`，分平移范数与
轴角旋转范数报告；并计算 Mahalanobis 距离平方 `ξᵀ Σ⁻¹ ξ`，与
χ² 阈值（默认 12.592，即 6 自由度 95% 分位）比较判定是否冲突。

---

## 3. API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 存活检查 + 签名公钥 |
| POST | `/audit` | 解析 + 传播 + 闭环检查（自动/显式枚举回路） |
| POST | `/chain` | 仅开环链复合与协方差传播 |
| POST | `/montecarlo` | Monte Carlo 核对一阶近似 |
| POST | `/sign` | 对任意 JSON 载荷做真实 Ed25519 签名 |
| POST | `/verify` | 校验 `{payload, signature, public_key}` |
| POST | `/calibration/ingest` | 验签 + 规范化指纹入库（演示） |

所有审计响应都带 `fingerprint_sha256`（canonical JSON 的 SHA-256）与
服务器 Ed25519 `signature`（公钥在 `public_key` 字段）。

### `/audit` 关键字段

* `correlation_policy`（**必填**）：`independent` / `conservative_rho` /
  `cross_blocks`，三选一，不能省略。
* `loops`：显式回路（帧行走，首尾相同）；留空则用 BFS 生成树枚举基本回路。
* 响应 `findings[]`：`illegal_rotation`、`covariance_not_psd`、
  `missing_covariance`、`loop_closure_conflict` 等，携带具体 `edge_id`、
  数值证据（最小特征值、`max|RᵀR−I|`、Mahalanobis² 等）。
* `summary.shortest_conflict_path`：**边数最少**的冲突回路（证据路径）。

---

## 4. 未知相关性：三种策略，绝不静默独立

`correlation_policy` 必填，独立只能被**显式声明**：

1. **`independent`**：显式假设各边扰动互不相关。响应中回显
   `independence_assumed` 提示，明确告知这是一个被采用的假设。
2. **`conservative_rho`**：相关性未知，给出相关系数上界 `ρ`（标量或逐边对
   矩阵，∈[0,1]）。对 PSD 联合协方差，由
   `[[I,I],[I,−I]]` 合同变换可证 `C + Cᵀ ≼ ρ(Si + Sj)`（Loewner 序），
   因而传播后
   `Σ += ρ(JkSkJkᵀ + JjSjJjᵀ)` 是对一切符合该界的交叉项的**保证 PSD 上界**。
   Monte Carlo 已验证该上界真实支配极端相干情形下的经验协方差（最小特征值
   间隙 ≥ 0，见 `tests/test_montecarlo.py::TestConservativeBound`）。
3. **`cross_blocks`**：调用方直接提供 6×6 交叉协方差块；装配成联合矩阵后
   整体校验 PSD，不一致的块给出 `cross_block_inconsistent`（含最小特征值）。

缺失协方差（`covariance: null`）时：传播结果置为 `indeterminate`，
闭环不给出"通过/冲突"结论，并明确列出缺哪几条边；**绝不按零处理**。

---

## 5. 最短证据路径

* **非法旋转** / **非正定协方差**：定位到具体 `edge_id`，附数值证据
  （`max|RᵀR−I|`、`det`、四元数范数、协方差最小特征值等），并立即中止传播
  （`fatal: true`），不会在坏数据上继续算出不可信结果。
* **闭环冲突**：枚举所有（显式或生成树基本）回路，计算每个回路的 Mahalanobis²，
  报告**边数最少**（同边数取矛盾最强）的冲突回路作为最短证据路径，逐边给出
  遍历方向（forward child→parent / reverse）。

---

## 6. Monte Carlo 与一阶近似的适用范围

`/montecarlo`（及 `tests/test_montecarlo.py`）做的是**真实的独立数值核对**：

1. 对每条边，按其 6×6 协方差从联合正态采样切向量 `ξk`（相关策略决定联合
   协方差的 Cholesky 因子）；
2. 用**精确 SE(3) 运算**施加扰动并复合整条链/环：
   右扰动 `Xk·Exp(ξk)`、左扰动 `Exp(ξk)·Xk`；
3. 对残差做精确 `log`，统计**经验协方差**；
4. 与仅使用伴随雅可比的**一阶解析协方差**对比，报告
   * 相对 Frobenius 误差 `‖Σemp − Σ1st‖F / ‖Σ1st‖F`，
   * 特征值比 `λemp/λ1st`，
   * 95% χ² 椭圆内样本比例（名义值 0.95）。

**实测结论**（种子固定，可复现）：

| 情形 | 单边旋转 std | 边数 | 相对 Frobenius 误差 | 95% 覆盖率 |
|------|-------------|------|--------------------|-----------|
| 短链/闭环，小角度 | 3e-3 rad | 2–3 | < 1.3% | ≈ 0.95 |
| 长链，小角度 | 2e-3 rad | 20 | < 2% | ≈ 0.95 |
| 短链，大角度 | 0.2 rad | 3 | 二阶项显现 | 明显 < 0.93 |
| 保守 ρ=0.8 上界 | 5e-3 rad | 3 | 上界 PSD 支配经验协方差 | — |
| 显式交叉块 ρ=0.5 | 5e-3 rad | 2 | < 1% | ≈ 0.95 |

**一阶近似适用范围**：每边扰动保持"小"——经验上旋转标准差 **≲ 约 0.02–0.05
rad（1–3°）**、平移标准差远小于连杆尺度时，短链与 20 边量级长链的误差都在
几个百分点内，95% 覆盖率落在 0.93–0.97。误差随单边扰动幅度的**二阶量**和
链长累积；当单边 std 达到 0.1–0.2 rad 量级时 BCH 二阶/三阶项不可忽略，
覆盖率显著偏低，此时应改用二阶传播、迭代重线性化或直接以 Monte Carlo 为准。
**缺失协方差时 Monte Carlo 拒绝运行**（不臆造噪声统计），需显式给出假设
协方差后重跑。

---

## 7. 目录结构

```
app/
  se3.py          SE(3)/SO(3)：exp/log、左雅可比、伴随、四元数
  linalg_utils.py PSD 校验、最近 PSD 投影、交叉块、保守包络传播
  graph.py        旋转校验、图解析、行走定向、雅可比、基本回路枚举
  montecarlo.py   真实采样的 Monte Carlo 一阶近似核对
  audit.py        /audit 与 /chain 的编排、证据与最短路径
  crypto.py       canonical JSON、SHA-256、Ed25519 真实签名/验签
  models.py       Pydantic 请求/响应模型
  main.py         FastAPI 应用
examples/         7 个示例输入
scripts/          keygen.py、sign_payload.py
tests/            53 个自动化测试（se3/audit/correlation/montecarlo/crypto/api）
requirements.lock 完整锁定依赖
```

## 8. 安全与诚实性说明

* 所有密码运算调用 `cryptography`（OpenSSL 后端）真实执行，无占位哈希/假签名；
  篡改载荷或换用别的公钥都会验签失败（有测试覆盖）。
* 私钥不入库（`.gitignore` 忽略 `keys/` 与 `*.pem`）。
* 计算失败、非法输入、非正定协方差、缺失协方差都如实返回相应状态与证据，
  不静默吞错、不伪造数值。
