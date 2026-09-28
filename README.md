# DID / Event-Time Panel Service

纯后端服务：对**两组两期**面板做差分中的差分（DID），并提供**事件时间**
（event-time）描述估计。所有数据均为本地合成夹具，无任何生产账号或外部服务依赖。

- 语言/框架：Python 3.12 · FastAPI · NumPy · SciPy · SQLite（标准库）
- 参考回归是**独立的第二条代码路径**（水平值闭式 OLS），不复用估计内核
- 参考答案是**手写**在 `fixtures/*.json` 里的四格均值/算术推导，**不是**由被测核心生成

---

## 1. 核心统计约定（这些是被强制的，不是口头假设）

1. **对象身份跨期对齐**：以 `object_id` 为身份，在两期间对齐。
   **缺期绝不补零**——某对象只出现一期，则从平衡样本中**排除**并记入
   `excluded_records`（类别 `identity_missing_period`）。
2. **平衡样本 + 固定权重**：对象仅在两期都被观测时进入；权重按对象固定
   （两期同为 1.0），不按期重新加权。
3. **标准误按对象聚类**：在两期一阶差分上做 CR1（Liang–Zeger）对象聚类
   三明治，自由度 = 对象数 − 1。
4. **平行趋势只能诊断、不能证明**：提供第三个更早的未处理期时，做安慰剂
   前趋势检验。检验*拒绝*是反对平行趋势的证据；*不拒绝*也明确**不构成证明**
   （反事实的后期趋势不可观测）。
5. **错时（staggered）超出支持模型即明确拒绝**：事件研究只支持“单一共同处理
   队列 + 从未处理对照”。检测到不同对象在不同期首次受处理，返回
   `event_stagger_unsupported` 并 `status="refused"`，而不是悄悄拟合会产生
   “禁用比较”的朴素 TWFE 事件系数。
6. **处理污染**：标记为对照但后期实际受处理的对象（`contaminated_control`），
   以及前期就已受处理的“处理组”对象（`pretrend_treated_early`），在估计前
   移入 `excluded_records`，不污染反事实。

统计上被**拒绝**的合法请求仍返回 HTTP 200，响应体 `status="refused"` 并带
机器可读的失败类别——拒绝是一类一等结果，而不是传输层错误；结构非法的请求
返回标准 422。

---

## 2. 工程结构（多模块，核心机制无硬编码演示）

```
app/
  contracts.py    统计契约：请求/响应模型、失败类别 FailureCategory
  config.py       本地配置（DB 路径、日志级别，环境变量可覆盖）
  panel.py        身份跨期对齐、缺期排除、(object,period) 重复排除、固定权重平衡样本
  kernel.py       估计内核：四格加权均值、DID 差分分解、对象聚类 CR1 标准误
  reference.py    独立参考回归：水平值 4 列设计矩阵闭式 OLS + 独立聚类三明治
  diagnostics.py  证据与诊断：处理污染筛查、前趋势安慰剂（只诊断不证明）
  event.py        事件时间对齐与单队列事件研究；错时超支持即拒绝
  experiments.py  复现实验：加载手写夹具、装配请求（不生成答案）
  storage.py      SQLite 审计：每次分析按 request_id 落库，可回查
  log.py          结构化 JSON 日志：关联 request_id/版本/处理位置，
                  失败原因与不确定结论分字段列出
  service.py      编排：panel → 污染筛查 → 内核 → 独立参考 → 诊断
  api.py          FastAPI 路由
fixtures/         手写夹具：原始数据 + 手算答案与算术推导（参考答案来源）
tests/            独立测试：断言具体数值与失败类别；含第三种朴素复算
scripts/
  reproduce.py        复现全部手写实验，逐值比对，非零退出码表示失败
  example_calls.sh    对运行中的服务发三个示例请求
```

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/did` | 两组两期 DID + 四格分解 + 聚类 SE + 独立参考回归 + 诊断 |
| `POST` | `/api/v1/event-study` | 单队列事件时间描述估计；错时拒绝 |
| `GET` | `/api/v1/runs/{id}` | 按行 id 回查审计记录（含请求/响应 JSON） |
| `GET` | `/api/v1/runs?request_id=` | 按请求身份回查 |
| `GET` | `/health` | 存活、版本、DB 位置 |

---

## 3. 安装与运行

依赖已锁定（见 `requirements.txt`，均为本机实际验证过的版本）：

```bash
pip install -r requirements.txt          # numpy / scipy / fastapi / pydantic / uvicorn / pytest / httpx

# 启动
uvicorn app.api:app --host 127.0.0.1 --port 8000
# 可选环境变量：DID_DB_PATH（审计库路径）、DID_LOG_LEVEL
```

交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`。

### `/api/v1/did` 请求示例

```json
{
  "request_id": "demo-handcalc",
  "pre_period": 1,
  "post_period": 2,
  "earlier_period": null,
  "observations": [
    {"object_id": "T1", "period": 1, "y": 10, "treated_group": true,  "treated_this_period": false},
    {"object_id": "T1", "period": 2, "y": 14, "treated_group": true,  "treated_this_period": true},
    {"object_id": "C1", "period": 1, "y": 8,  "treated_group": false, "treated_this_period": false},
    {"object_id": "C1", "period": 2, "y": 9,  "treated_group": false, "treated_this_period": false}
  ]
}
```

响应关键字段：`decomposition`（四格均值与两级差分）、`clustered_se`、
`reference_regression`（独立 OLS 及 `max_abs_did_discrepancy`）、`pretrend`、
`contamination`、`excluded_records`、`failures`、`method_notes`、`summary`
（含 `processing_location`、版本、计数）。

```bash
# 终端内直接对运行中的服务跑三个示例（手算 DID / 单队列事件 / 错时拒绝）
BASE=http://127.0.0.1:8000 bash scripts/example_calls.sh
```

---

## 4. 复现与独立测试

```bash
python scripts/reproduce.py     # 手写夹具逐值比对，全部 PASS
python -m pytest -q             # 22 个测试
```

### 可复核的四个手算样例

| 夹具 | 核验内容 |
| --- | --- |
| `handcalc_2x2.json` | 四格均值 11 / 14.5 / 9 / 10.5，处理组变化 3.5、对照组 1.5、**DID=2.0**；手算聚类 SE=1/√3，t=3.464，df=3 |
| `missing_period.json` | 两个缺期对象被排除且**不当零**；平衡样本仍得 DID=2.0 |
| `nonparallel_pretrend.json` | 处理前两期两组趋势已分化（差 8，p≈0.0008），前趋势**被拒绝**；DID=3.0 仍返回但带警告 |
| `contamination.json` | 污染对照 `CX_CONTAMINATED`（post=50）与提前处理 `TX_EARLY` 被排除；干净样本 DID=2.0 |
| `event_single_cohort.json` | 事件时间 −3..1：处理前各期为 0，处理当期/下期为 **+5** |
| `event_staggered_refused.json` | 队列 {2,2,3} → `event_stagger_unsupported`，`refused` |

独立性体现在三处一致：**手写答案**（夹具内附算术推导）、**测试内第三种朴素
dict 复算**、以及服务的**内核 + 独立水平值 OLS**。测试断言的是具体数值、自由度、
失败类别与被排除对象，而不是“接口能调用”。

---

## 5. 可解释性

- 每个响应都回带 `request_id`、`service`、`version`，`summary.processing_location`
  给出主机名与 Python 版本。
- 每次调用在 SQLite 表 `analysis_runs` 落库（请求+响应 JSON、端点、状态、版本、
  主机、时间戳），可用 `/api/v1/runs` 回查。
- 日志为单行 JSON，分步记录 `received` / `completed`、耗时、`run_id`；
  **失败原因**在 `failures`，**不确定结论**（如“平行趋势未被拒绝≠被证明”、
  “无法检验”）单列在 `uncertainties`。

---

## 6. 剩余限制（如实说明）

- **错时 DID 只拒绝、不估计**：未实现 Callaway–Sant’Anna、Sun–Abraham、
  堆叠回归等异质稳健估计量；落在支持模型外时服务明确报错而非给数。
- 权重机制为**对象固定等权**；未开放用户自定义权重、频数权重或调查权重。
- 聚类三明治为经典 **CR1**、t 参考分布 df=G−1；未实现 CR2/野生 Bootstrap 等
  小样本校正，故对象数很小时推断需谨慎（服务会把单组单聚类标记为方差退化）。
- 合成数据可能“精确拟合”（组内残差恰为 0），此时聚类方差估计恰为 0；服务会在
  `note` 中说明，而非伪造一个标准误。
- 协变量调整、匹配/加权 DID、多处理剂量、空间溢出/SUTVA 违背等均未覆盖。
- 服务无鉴权、无速率限制，定位为**本地**分析服务；请勿直接暴露到公网。
