# IPW-ATE — 交叉拟合逆概率加权的处理效应估计与重叠诊断

面向**本地合成数据**的研究型工程：用交叉拟合（cross-fitting）的
逆概率加权（IPW）估计平均处理效应（ATE/ATT/ATU），并对**重叠（overlap）**
做可审计的诊断。所有结论都条件于无未测混杂、正性、一致性假设；
本工程**不**把估计值当作因果证明。

技术栈：Python 3.10+ · NumPy · SciPy · FastAPI · SQLite（仅标准库与这些依赖）。

---

## 1. 它明确承诺什么（统计契约）

每次运行都在结果里固化并回传一份 `contract`，离开契约无法解读数字：

- **目标人群**：`ate`（全体）、`att`（处理组）、`atu`（未处理组）。
- **权重定义**：
  - 稳定权重 ATE：`w = A·p̄/p̂(X) + (1-A)·(1-p̄)/(1-p̂(X))`，`p̄=P_n(A=1)`；
  - Horvitz–Thompson：`w = A/p̂(X) + (1-A)/(1-p̂(X))`；
  - ATT：处理组权重为 1，未处理组为 `p̂/(1-p̂)`（稳定化乘 `(1-p̄)/p̄`）；
  - ATU：未处理组权重为 1，处理组为 `(1-p̂)/p̂`（稳定化乘 `p̄/(1-p̄)`）。
  - 点估计用自归一化（Hájek）加权均值；标准误用影响函数（influence function）。
- **交叉拟合**：K=5 个分层折（种子写死在配置，可复现）；每个单元的倾向得分
  只由**没见过该单元**的模型预测（OOF）。
- **倾向模型**：岭惩罚逻辑回归（IRLS，可逐行审计），截距不罚、协变量按
  **训练折**统计量标准化。
- **权重截断**：默认关闭；开启时使用**单一固定档位**
  `v1_fixed_p0p02_p0p98`（p∈[0.02,0.98]），不做随数据自适应的分位数截断。
  开启即声明目标改为“修剪后人群”，契约与结果中明确写出。
- **禁止静默分母替换**：拟合得分使相关分母 ≤ `positivity_eps(=1e-6)` 时，
  直接以失败类别 `positivity_violation` 拒绝；不做 epsilon 替换、不兜底。
- **假设与免责**：结果内嵌一致性/条件可交换性/正性/SUTVA 的文字声明与
  `causal_disclaimer`，并说明无未测混杂**不可由数据检验**。

## 2. 模块职责（非单文件、非空壳）

```
src/ipwate/
  statcontract.py  统计契约、结果结构、假设与因果免责声明（只声明“主张什么”）
  config.py        YAML 配置加载与 API 白名单覆盖（估计内核不读裸字典）
  errors.py        稳定错误码（失败类别）：positivity_violation / validation_error / ...
  validation.py    系统边界严格校验（形状、NaN、二值处理、组数、折数）
  splits.py        确定性分层交叉拟合折（可复现、互斥且恰好覆盖一次）
  model.py         岭逻辑回归 IRLS（审计友好；收敛失败显式报错，不裁剪预测）
  weights.py       权重/正性/固定截断/ESS/影响函数方差 —— 纯数学内核
  diagnostics.py   重叠、ESS、极值权重、校准（Hosmer–Lemeshow、斜率）与脱敏结构化日志
  pipeline.py      交叉拟合编排：校验→OOF 训练→权重→诊断→组装证据
  synthetic.py     已知 DGP 的合成数据（真值/机制独立给出，供对照）
  storage.py       SQLite 审计落库（只存结果 JSON，不存原始观测）
  api.py           FastAPI：/healthz、/api/v1/ipw/estimate、/api/v1/runs/{id}
  cli.py           本地复现入口
configs/default.yaml  声明式契约（阈值、档位、种子）
experiments/           复现脚本与夹具生成
examples/              服务调用示例（httpx 与 curl）
tests/                 独立测试（断言具体数值与失败类别）+ 手算夹具
```

## 3. 安装

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.lock   # 或 -r requirements.txt
python -m pip install -e .
```

## 4. 运行测试（含覆盖率）

```bash
python -m pytest --cov=ipwate --cov-report=term-missing
```

当前 **57 个测试全部通过，总覆盖率约 95%**，每个源文件均 ≥80%。

### 测试为什么是“可核验”的

- **手算小样本**：`tests/fixtures/tiny_weights.json` 的期望值由纸笔算术得到
  （推导见下文第 8 节），`tests/test_weights_kernel.py` 断言到 1e-12，
  包括 ATE/ATT/ATU 权重、Hájek 均值与 Kish ESS。参考答案**不是**被测实现产出的。
- **失败类别**：正性违背、单类折、模型不收敛、非二值处理、NaN、越权契约字段等，
  都断言到具体错误码与 details，而不是“能调用不报错”。
- **已知生成过程**：合成器暴露真值 `tau(X)`；测试断言点估计贴近真值、
  100 次蒙特卡洛 95% 区间覆盖率、错设模型被校准检查拒绝。
- **独立数据通路**：`tests/test_csv_fixture.py` 从签入的 CSV 读入，
  并用 DGP 公式在测试内**重算**真值。

## 5. 复现实验

```bash
PYTHONPATH=src python experiments/reproduce.py        # 重新生成 reports/ 全部产物
PYTHONPATH=src python experiments/make_fixture.py     # 重新生成 CSV 夹具（会改变字节，勿随意执行）
```

`reports/` 已包含可复核产物（固定种子）：

| 文件 | 场景 | 结论 |
|---|---|---|
| `good_overlap.json` | 重叠良好 | 点估计 2.008 vs 真值 2.0085，verdict=accept |
| `misspecification.json` | 倾向模型错设（真值非线性） | verdict=**reject**（`calibration_hl_reject`），即使点估计恰好接近 |
| `poor_overlap_clipped.json` | 重叠差 + 固定截断 | verdict=warn，622 个得分越界，契约声明修剪后人群 |
| `no_overlap_error.json` | 经验正性违背 | rc=2，`positivity_violation`，给出越界计数 |
| `coverage_summary.json` | 100 次重复 | 95% 区间经验覆盖 0.98（MC SE≈0.022），平均偏差≈0.002 |

CLI 单跑：

```bash
PYTHONPATH=src python -m ipwate.cli --scenario good_overlap --n 4000 --seed 2024 --show-true-ate
PYTHONPATH=src python -m ipwate.cli --scenario no_overlap   # 退出码 2 + 结构化错误
```

## 6. 服务调用

```bash
PYTHONPATH=src python -m uvicorn ipwate.api:app --port 8000
# 另一终端
PYTHONPATH=src python examples/service_calls.py
# 或见 examples/curl_examples.md
```

错误响应统一信封：`{"ok": false, "request_id": ..., "error": {"code", "message", "details"}}`。
`x-request-id` 头贯穿日志与响应；诊断日志只打印**脱敏聚合状态**
（样本量、协变量数、各组 n、ESS、极值权重、finding 码），绝不打印 X/Y 原始值。

诊断裁定优先级：`reject > warn > inconclusive > accept`，每条 finding 带
`code / severity / status / message / evidence`，说明**为什么**接受、警告、
拒绝或无法判定（小样本校准返回 inconclusive 而非假装通过）。

## 7. 诊断与判定阈值（可在 configs/default.yaml 审计）

- 每臂 ESS 占比：<2% 拒绝，<10% 警告（Kish ESS = (Σw)²/Σw²）。
- 最大权重：>100 拒绝，>20 警告。
- 处理/未处理倾向得分 5%–95% 区间：不相交→无共同支撑（拒绝）；交叠很薄→警告。
- 校准：Hosmer–Lemeshow（10 组，p<0.01 拒绝、p<0.05 警告）+ 校准斜率
  （允许带 [0.5,1.5]）；样本 <200 不做结论（inconclusive）。

## 8. 手算夹具推导（参考答案独立来源）

四单元（`p̄=P(A=1)=0.5`）：

| i | A | p̂ | Y |
|---|---|---|---|
|1|1|0.25|4|
|2|1|0.50|2|
|3|0|0.25|0|
|4|0|0.50|1|

稳定 ATE 权重：处理组 `0.5/p̂` = `[2,1]`，未处理组 `0.5/(1-p̂)` = `[2/3,1]`。
`μ1=(2·4+1·2)/(2+1)=10/3`，`μ0=((2/3)·0+1·1)/(2/3+1)=0.6`，ATE=3.333−0.6=**2.7333**。
处理臂 ESS=`3²/5=1.8`，未处理臂=`(5/3)²/(13/9)=25/13≈1.9231`。
ATT：处理组权重 1 → μ1=3；未处理 `p̂/(1-p̂)·(1-p̄)/p̄`=`[1/3,1]` → μ0=0.75 → **2.25**。
ATU：未处理权重 1 → μ0=0.5；处理组 `(1-p̂)/p̂·p̄/(1-p̄)`=`[3,1]` → μ1=3.5 → **3.0**。
这些数字逐字写在夹具里，内核测试以 1e-12 断言。

## 9. 边界与注意事项

- 仅用于本地合成/受控数据；不采集真实业务数据、不需要生产账号。
- 诊断能“拒绝/警示”薄弱支撑，但**不能证明**无未测混杂或正性成立。
- 截断改变估计目标；解读截断结果时必须连同 `estimand_note` 一起报告。
- 影响函数 SE 忽略了倾向模型的高阶项（交叉拟合下的标准近似）；需要更稳健的
  推断时应做基于折的/bootstrap 复核。
