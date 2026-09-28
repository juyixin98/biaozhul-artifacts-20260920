# 合成线性模型 2SLS 服务与弱工具诊断

一个可运行的 **两阶段最小二乘（2SLS / IV）估计服务**：FastAPI + NumPy/SciPy + SQLite，
全部数据使用本地合成夹具（DGP），无外部业务依赖。服务不仅给出系数，还**显式报告每一个
接受 / 拒绝 / 无法判定的裁决及其关键状态**。

---

## 1. 它解决什么问题

结构方程存在内生解释变量时 OLS 不一致；用工具变量做 2SLS 必须回答四个相互独立的问题：

1. **识别**：阶条件（工具数 ≥ 内生变量数）与秩条件（工具对内生变异有真实相关性）；
2. **工具强度**：识别成立但工具很弱时，2SLS 偏向 OLS、标准误失真（弱工具不是"报错"，
   而是必须随结果一起呈现的诊断）；
3. **工具有效性（外生性）**：过度识别检验仅在 L>k 时可做，且**永远不能证明**排除限制；
4. **内生性本身**：处理变量是否真的内生（Durbin–Wu–Hausman 类检验）。

本服务把这四件事做成有真实职责的独立模块，并把**排除限制作为调用方的经济假设记录在案**，
不会因为第一阶段相关系数高就"自动推出"工具外生。

### 统计记号

```
结构方程   y = Y β + X γ + e          Y:(n,k) 内生  X:(n,m) 含外生控制/常数项
第一阶段   Y = X Πx + Z Πz + V        Z:(n,L) 被排除工具
阶条件     L ≥ k（必要非充分）
秩条件     rank(M_X Z' M_X Y) = k（数值检验）
```

### 估计内核与"正确的标准误"

2SLS 点估计来自 IV 正规方程（通过交叉积计算，从不显式形成 n×n 投影矩阵）：

```
W=[Y,X], Q=[X,Z], P_Q = Q(Q'Q)^{-1}Q'
δ̂ = (W'P_Q W)^{-1} W'P_Q y
结构残差  ê = y − W δ̂          （注意：不是第二阶段 y − [Ŷ,X]δ̂ 的残差！）
同方差方差   σ²·(W'P_Q W)^{-1},  σ² = ê'ê/(n−k−m)
稳健 HC1     B · W'P_Q diag(ê²) P_Q W · B,  B=(W'P_Q W)^{-1}
```

**关键点**：直接把 `y` 对 `[Ŷ, X]` 跑普通 OLS 得到的标准误是错的——它用的残差方差基于
`Ŷ≠Y`。本服务的方差使用**结构残差**。测试 `test_standard_errors_are_NOT_naive_second_stage_ols`
显式断言内核结果与朴素第二阶段 OLS 公式不同、而与独立的结构残差矩阵公式一致。

---

## 2. 模块布局（不是单文件脚本，也不是空接口工程）

```
src/twosls/
  config.py          环境可覆盖的配置（规模护栏、秩容差、弱工具阈值）
  errors.py          错误分类法：稳定的 code + request_id + key_state
  contract.py        统计契约：请求/校验数据/结果的 pydantic 与 dataclass
  data.py            边界校验：载荷 → 已验证矩阵（维度、有限值、角色互斥、自由度）
  linalg.py          投影/湮灭矩阵、对称伪逆、SPD 求解、数值秩
  kernel.py          估计内核（只做数学）：点估计、结构残差、两种协方差
  identification.py  阶/秩条件、第一阶段 F、偏 R²、Sanderson–Windmeijer 条件 F、Cragg–Donald
  validity.py        Sargan / Wooldridge 稳健得分、Durbin–Wu–Hausman 内生性检验
  estimator.py       编排：内核+诊断 → 状态裁决（ok/weak/inconclusive）、自助法
  evidence.py        SQLite 证据库：接受与拒绝都落库，只存统计量不存原始观测
  serialize.py       结果 → JSON（含 decision_summary 说明"为什么"）
  api.py             FastAPI 路由、错误信封、X-Request-ID
  __main__.py        uvicorn 入口
experiments/
  dgp.py             合成 DGP：已知内生性/弱工具/共线工具/秩失败/无效工具/多内生
  replicate.py       蒙特卡洛复现实验（E1–E5），产出 reports/
scripts/
  demo_local.py      不走 HTTP 的本地端到端演示（全部裁决类别）
  demo_http.sh       起服务 + curl 完整调用（自动选空闲端口）
tests/
  reference/         ★独立参考答案（与内核零共享代码路径）
  test_kernel.py     矩阵公式/数值 GMM/ILS/自助法 对照
  test_diagnostics.py 断言具体数值与失败类别
  test_validation.py 边界非法输入分类
  test_api.py        HTTP 集成（TestClient）
  test_linearmodels_oracle.py  可选：对照成熟实现 linearmodels
```

### 参考答案为什么不是"被测核心自己生成的"

`tests/reference/` 用**四条互相独立**的路径复算答案，与 `kernel.py` 不共享任何代码：

1. **稠密投影公式**——显式构造 n×n 的 P_Q 并用 LAPACK `inv/solve`（内核用特征分解对称逆、
   从不显式形成 P_Q）；
2. **数值 GMM 优化**——用带解析梯度的 BFGS 极小化 GMM 目标，靠优化器而非闭式解到达同一点；
3. **间接最小二乘（ILS）**——恰好识别情形经简化式参数独立推导；
4. **独立 RNG 的配对自助法**。

另提供 `linearmodels`（独立维护的成熟包）对照测试，未安装时自动 skip。

---

## 3. 诊断与裁决语义

结果顶层 `status`：

| status | 含义 | 处置 |
|---|---|---|
| `ok` | 阶/秩成立且强度过阈值 | 接受点估计 |
| `weak` | 形式上可识别但弱工具（条件 F / Cragg–Donald / 偏 R² 触发） | **仍返回估计**，但带显著告警；`options.strict=true` 时改为拒绝 |
| `inconclusive` | 识别成立但有效性诊断（如过度识别拒绝）使结构解释不成立 | 返回数值但 `accepted=false` |
| （抛错）`NOT_IDENTIFIED` | 阶条件或秩条件失败，点估计数学上未定义 | 不返回估计 |

具体诊断量：

- **阶条件** `L >= k`；**秩条件** 用构成矩阵范数做绝对参照
  `σ_min(Zt'Yt) > tol·σ_max(Zt)·σ_max(Yt)`（纯自相对容差会把"整体都极小"归一化掉，
  这是被测试逼出来的一个真实修复点）。
- **第一阶段 F**（经典 F(L, n−m−L)）、**偏 R²** 与调整偏 R²。
- **Sanderson–Windmeijer 条件 F**（k>1 时关键）：把第 j 个内生变量对**其他内生变量实际值**
  与工具回归，再检验工具联合显著——能识别"边际 F 都很大但第一阶段系数矩阵列近共线、
  结构系数联合不可识别"的情形。
- **Cragg–Donald** g_min 特征值统计量。
- **Sargan**（同方差）/ **Wooldridge 稳健得分**（异方差），χ²(L−k)，仅 L>k 可检验；
  非拒绝明确标注"不等于证明外生"。
- **Durbin–Wu–Hausman** 增广回归检验（协方差随请求的 homoskedastic/robust 选项），
  并同时报告 OLS 与 IV 系数对比。

### 排除限制的处理

请求体里的 `validity_claim.exclusion_restriction_asserted` + `rationale` 是**调用方提供的
经济假设**，被原样记录、在结果 `assumptions.exclusion_restriction` 中回显，并明确
`derivable_from_data=false`。任何相关性/ F 统计量都不会把它改成"已验证"。

---

## 4. HTTP 接口

```
GET  /health                       存活与阈值快照
POST /api/v1/iv/estimate           执行 2SLS + 全套诊断
GET  /api/v1/runs/{request_id}     取某次运行的证据（含被拒绝的请求）
GET  /api/v1/runs?limit=           列出最近运行
```

请求示例（节选）：

```json
{
  "request_id": "demo-1",
  "columns": {"y": [...], "x_end": [...], "z1": [...], "z2": [...], "w1": [...]},
  "spec": {
    "dependent": "y",
    "endogenous": ["x_end"],
    "included_exogenous": ["w1", "const"],
    "excluded_instruments": ["z1", "z2"]
  },
  "options": {"covariance": "homoskedastic", "strict": false, "confidence_level": 0.95},
  "validity_claim": {
    "exclusion_restriction_asserted": true,
    "rationale": "synthetic DGP: Z generated independently of structural error e"
  }
}
```

响应含 `coefficients`（估计/标准误/t/p/置信区间，可选自助标准误）、`first_stage`、
`identification`、`overidentification`、`endogeneity`、`assumptions`、`warnings`，以及
`decision_summary.accepted` 与 `decision_summary.why`（逐条说明裁决依据）。

### 错误语义

所有错误都是同一信封：`{"error": {"code", "message", "request_id", "key_state"}}`。

| HTTP | code | 何时产生 |
|---|---|---|
| 422 | `VALIDATION_FAILED` | 载荷非法：缺列/列角色重叠/长度不一致/NaN/Inf/观测太少/零方差列等 |
| 422 | `NOT_IDENTIFIED` | 阶条件或秩条件失败，点估计未定义（`key_state` 给出秩、所需秩、原因） |
| 422 | `WEAK_INSTRUMENTS` | 弱工具且请求 `strict=true`（非严格时不报错，返回 status=weak） |
| 422 | `DEGENERATE_DATA` | 统计量无定义（残差零方差等） |
| 500 | `ESTIMATION_FAILED` | 内核未预期错误（不泄漏内部细节） |
| 404 | `NOT_FOUND` | 查询的 request_id 无记录 |

### 隐私与可追溯

- 每条日志和错误都带 **request_id**（并回写 `X-Request-ID` 响应头）。
- 日志里矩阵只记录 **形状 + 有限单元数 + 内容 SHA256 前 12 位**，从不打印观测值；
  错误与证据库同样只含维度与统计量。
- 接受和拒绝都会写 SQLite（`data/twosls.db`，可用 `TWOSLS_DB_PATH` 覆盖）。

---

## 5. 复现步骤

```bash
# 依赖（本机已具备 numpy/scipy/fastapi/uvicorn/pydantic/pytest/httpx）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 核心 + 测试
.venv/bin/pip install -r requirements-optional.txt # 可选：linearmodels 对照

# 1) 测试（系统 Python 即可，无需装包）
python3 -m pytest tests/ -q
#   装了 linearmodels 时会额外多出 3 个成熟实现对照用例：
.venv/bin/python -m pytest tests/ -q

# 按标记选择
python3 -m pytest tests/ -m unit -q
python3 -m pytest tests/ -m diagnostics -q
python3 -m pytest tests/ -m api -q

# 2) 本地端到端演示（不走网络，覆盖全部裁决类别）
PYTHONPATH=src python3 scripts/demo_local.py

# 3) HTTP 服务 + curl（自动选空闲端口、临时数据库）
bash scripts/demo_http.sh
#    或手动：
PYTHONPATH=src python3 -m uvicorn twosls.api:app --host 127.0.0.1 --port 8011

# 4) 蒙特卡洛复现实验 -> reports/replication.{json,md}
PYTHONPATH=src:. python3 experiments/replicate.py --quick   # 快速冒烟（80 次）
PYTHONPATH=src:. python3 experiments/replicate.py --reps 400
```

### 复现实验结论（400 次，见 reports/replication.md）

- **E1 一致性**：强工具下 2SLS 在 n=1000/4000 收敛到真值 β=0.75（偏差≈0.00），
  OLS 持续偏在 ~1.10（偏差 +0.35）；弱工具下 2SLS 均值虽近无偏（+0.003）但 RMSE 高达
  0.41、93% 被标记 weak——正是"弱工具下分布发散"的典型表现。
- **E2 覆盖率**：同方差误差下两种 SE 覆盖率均 ~0.95；**异方差误差下朴素同方差 SE 覆盖率
  跌到 0.86，而稳健 SE 维持 0.94**，验证了 robust 选项的必要性与正确性。
- **E3 Sargan**：有效工具下经验水平 0.058（名义 5%）；污染工具下功效 1.00。
- **E4 内生性检验**：已知内生时功效 1.00；外生时经验水平 0.043（名义 5%）。
- **E5 失败类别**：秩失败/重复工具→unidentified，总体零 π/弱工具→weak，共线但满秩→ok，
  全部符合预期。

阈值（弱工具 F=10、偏 R²=0.10、秩容差等）可通过环境变量 `TWOSLS_WEAK_F`、
`TWOSLS_PARTIAL_R2_LOW`、`TWOSLS_RANK_TOL` 等覆盖，见 `src/twosls/config.py`。
