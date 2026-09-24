# 标定链传播审计 (Calibration Chain Propagation Audit)

纯后端服务：输入一组坐标系变换（传感器外参）、每条边的 **6 维小扰动协方差**
与**标定版本**，在**统一的左/右扰动约定**下沿变换链做一阶协方差传播，
对每个闭环做 χ² 闭合检验，审计标定版本混用，并用 **Monte Carlo 合成样本**
核对一阶传播近似的适用范围。

技术栈：Python 3.10+ · NumPy（SE(3) 数学/抽样全部手写实现）· FastAPI ·
Ed25519 真实签名。无前端、无 scipy（χ² 生存函数用闭式）。

---

## 1. 它做什么

- **SE(3) 外参链传播**
  - 6 维扰动向量序 `[v; ω]`（前 3 平移、后 3 旋转轴角，rad）。
  - **右扰动** `T' = T·Exp(ξ)`（默认，扰动在 child/body 帧）；
    **左扰动** `T' = Exp(ξ)·T`（扰动在 parent/world 帧）。全链统一一种。
  - 沿有向边（含逆边）计算名义链乘积与扰动雅可比 `J`，一阶
    `Σ_out = J·blockdiag(Σ_k)·Jᵀ`（独立）或含交叉项的版本。
- **闭环检验**：对生成树之外的每条非树边构成一个基本环，计算闭环残差
  `log(L)`、协方差、Mahalanobis 距离与 χ²₆ 上尾 p 值，`p < α` 判冲突，
  并给出环上的**边证据路径**。
- **非法/不确定输入的最短证据**
  - 非正交 / det≠1（反射）/ 未归一化四元数 / 非有限值 → `ILLEGAL_ROTATION`；
  - 协方差不对称 → `COVARIANCE_NOT_SYMMETRIC`；
    最小特征值为负 → `COVARIANCE_NOT_PSD`，附最负特征值与特征向量；
  - 每个问题都带 `evidence_path`（定位到具体 `edge:<id>` / `node:<id>`）。
- **未知相关性不默认独立**
  - `correlation_policy` **必填**；
  - `independent`：显式声明各边扰动独立（报告回显被当作独立的每一对边）；
  - `bounded`：必须给 `rho_max∈[0,1]`，在**输出空间**构造等相关保守上界
    （`ρ=1` 时逐方向 Cauchy–Schwarz 收紧，PSD 意义下不小于独立结果）；
  - `explicit`：逐对给 6×6 互协方差，未列出者必须声明
    `unspecified_policy`（independent / bounded），同样回显，绝不沉默假设。
- **缺失协方差**：链上任何一条边缺协方差，相关位姿/闭环的协方差状态置
  `unknown`，只报名义残差、**不做统计判定、不冒充独立**。
- **标定版本**：同一坐标系汇集多个版本时输出 `version_mismatches` 及证据。
- **真实性**：每份审计结果用服务 Ed25519 私钥对规范化 JSON 签名，附
  SHA-256 摘要与公钥；`/verify` 真实验签，可检测篡改。
- **Monte Carlo 自检**：真实抽样（向量化 SE(3)）合成带噪链，统计样本协方差
  与一阶 `JΣJᵀ` 对照，覆盖**小角度 / 长链 / 缺失协方差 / 相关性 / 大扰动失效**。

---

## 2. 目录结构

```
app/
  se3.py          SE(3)/SO(3) exp/log、Ad、四元数（纯 NumPy，数值稳定）
  validation.py   旋转合法性与 6x6 协方差半正定性校验，产出证据路径
  chain.py        图、BFS、因子链、雅可比、相关性聚合、生成树、基本环、χ² 检验
  montecarlo.py   批量 SE(3) 抽样与一阶传播对照
  scenarios.py    MC 自检场景（小角度/长链/缺失/相关性/失效反例）
  crypto.py       Ed25519 签名/验签、SHA-256、规范化 JSON、密钥持久化
  models.py       Pydantic 请求/响应/错误协议
  engine.py       审计编排（校验→传播→闭环→版本→相关性声明）
  main.py         FastAPI 应用：/health /audit /verify /montecarlo/selfcheck
examples/         4 个可直接 POST 的示例输入
tests/            77 个 pytest 用例（数学/校验/传播/API/密码）
scripts/accept.sh 一键验收（起服务、真实调用、验签、MC）
requirements.txt  顶层依赖（精确钉版本）
requirements-lock.txt  完整传递依赖锁定（pip freeze）
```

---

## 3. 本地启动

需要 Python 3.10+（在 3.12 验证）。

```bash
cd /home/admin/Downloads/biaozhul/P060/b

python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 锁定依赖，可复现
# 或装顶层钉版本： .venv/bin/pip install -r requirements.txt

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8791
```

启动时若 `keys/ed25519_private.pem` 不存在会自动生成（权限 0600）。
交互式 API 文档：<http://127.0.0.1:8791/docs>。

---

## 4. 验收命令

```bash
# 一键：77 个单测 + 起服务 + 真实审计/验签/防篡改 + MC 自检
./scripts/accept.sh            # 默认端口 8791，可用 PORT=xxxx 覆盖
```

也可分步：

```bash
.venv/bin/python -m pytest tests/ -q                 # 只跑自动化测试

.venv/bin/python -m uvicorn app.main:app --port 8791 &
curl -s http://127.0.0.1:8791/health

curl -s -X POST http://127.0.0.1:8791/audit \
  -H 'Content-Type: application/json' \
  --data @examples/audit_chain.json

curl -s -X POST http://127.0.0.1:8791/montecarlo/selfcheck?n_samples=20000
```

---

## 5. API 协议摘要

### `POST /audit`
请求关键字段：

| 字段 | 说明 |
|---|---|
| `root_frame` | 传播根坐标系 |
| `convention` | `right`（默认）/ `left`，全链统一 |
| `edges[]` | `id, parent, child, transform{translation,rotation}, covariance?(6x6), version` |
| `correlation_policy` | **必填**：`independent` / `bounded` / `explicit` |
| `rho_max` | `bounded` 必填，`[0,1]` |
| `cross_covariances[]` | `explicit` 用，逐对 6×6 |
| `unspecified_policy` | `explicit` 必填：未列出边对按 independent/bounded |
| `alpha` | χ² 检验显著性，默认 0.01 |

`rotation` 可为 3×3 矩阵或四元数 `[w,x,y,z]`。
`covariance` 省略 = 该边协方差缺失（结果置 unknown，不参与统计判定）。

响应是**签名信封**：`request_id / timestamp / algorithm=ed25519 / key_id /
public_key / signed_payload_sha256 / signature / result`。
`result.overall.status` 为 `ok` 或 `conflict`；`loops[]` 含每条环的残差、
χ²、p 值、证据路径、各边版本；`correlation_declaration` 显式列出每对边
被如何处理。

错误统一 `422`：`{"error":{"type":..., "issues":[{"code","message",
"evidence_path","details"}]}}`。

### `POST /verify`
入参 `{public_key, signature, result}`，用自带公钥验 Ed25519；
载荷被改一个字节即返回 `valid:false`（HTTP 400）。

### `POST /montecarlo/selfcheck?n_samples=&seed=`
返回各场景一阶 vs 样本协方差的 Frobenius 相对误差、trace 比值与
`summary.passed`，以及 `validity_range` 适用范围文字。

---

## 6. 数学与约定（要点）

- 雅可比经一阶共轭恒等式严格推导，并用有限差分对**左/右约定 × 正/逆因子 ×
  随机链**逐块验证（误差 ~1e-9）。
  - 右约定因子 k 系数 `Ad_{tail⁻¹}·local_k`，`tail=F_{k+1}…F_n`；
  - 左约定因子 k 系数 `Ad_{head}·local_k`，`head=F_1…F_{k-1}`；
  - 逆因子边→因子扰动：右 `-Ad_E`，左 `-Ad_{E⁻¹}`。
- χ²₆ 生存函数闭式（偶数自由度）：
  `P(χ²₆>x)=e^(−x/2)(1+x/2+x²/8)`，无需 scipy。
- `bounded` 的交叉项在输出空间构造为 `ρ A_k^{1/2} A_l^{1/2}`
  （`A_k=J_kΣ_kJ_kᵀ`），从而任意方向满足 Cauchy–Schwarz；
  `ρ=1` 给出无条件保守上界。
- 协方差物理上允许半正定（某方向零不确定度）；< −1e-10 判非法，
  1e-10 内的微小负特征值视为舍入钳为 0。

---

## 7. 一阶近似适用范围（Monte Carlo 实测）

`POST /montecarlo/selfcheck` 用合成样本给出实测边界（样本量默认 3 万）：

| 场景 | σ_rot | 链长 | Fro 相对误差 |
|---|---|---|---|
| 小角度（右/左） | 1e-3 rad | 4 | ~1.3–1.6% |
| 长链小噪声 | 1e-3 | 12 | ~1.5% |
| 长链中等噪声 | 3e-2 | 12 | ~1.3% |
| 等相关 ρ=0.5 | 1e-3 | 4 | 上界 trace 比值 ≤ 1（保守） |
| **大扰动失效反例** | **0.3 rad** | **12** | **~10–15%** |
| 缺失协方差 | — | 4 | `unknown`（不输出数值结论） |

**结论**：当每条边扰动保持“小”（经验上 σ_rot ≲ 3×10⁻² rad、链上累计旋转
≲ 0.2–0.3 rad）时，`JΣJᵀ` 一阶传播与 Monte Carlo 样本协方差的 Frobenius
相对误差 < 2%。误差来自 SE(3) 的 BCH 二/三阶项，随 σ² 与链长增长；
σ_rot≈0.3 rad、12 边时升到约 10–15%，此时应改用无迹/二阶方法或分段线性化。
缺失协方差的环不参与任何统计结论。

数值边界：闭环旋转角接近 π（θ=π−δ，δ~1e-7）存在固有不可区分性，旋转矩阵
重构误差约 2e-9；审计同时看平移残差与整体 Mahalanobis 距离，不依赖单一角度。

---

## 8. 失败如实报告

- 所有计算（SE(3)、传播、χ²）、协议解析、密码签名/验签均为真实执行；
  无 mock、无占位哈希。
- 校验不过返回 422 并附每条问题的最短证据；闭环冲突不静默；缺失协方差
  明确 `unknown`；签名/验签结果真实反映篡改。
- 若启动时密钥目录不可写，`Signer.load_or_create` 会抛错使服务启动失败，
  而非降级为不签名。
