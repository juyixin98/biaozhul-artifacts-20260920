# DID Panel Service

纯后端服务：对**合成面板数据**做两组两期双重差分（DID）与事件时间（event-time）
描述性合成。Python · FastAPI · NumPy · SciPy · SQLite，全部数据与依赖本地化，
无需任何生产账号或真实业务数据。

它不是一个"硬编码演示"：统计契约、估计内核、证据与诊断、复现实验是四个独立模块，
参考答案由一个**不导入被测内核**的独立实现（裸 NumPy 正规方程 + 手写三明治）和
纸上常量共同给出。

---

## 1. 它保证的统计纪律

- **对象身份跨期对齐，缺期绝不补零**——缺测是独立状态，默认平衡样本下排除并记录。
- **平衡策略与权重估计前固定**；`unit_fixed` 权重冻结在基线期、两期复用。
- **标准误按对象（unit）聚类**（CRV1，参考分布 t(G−1)）。
- **平行趋势只能诊断、不能证明**：前置趋势检验可证伪；不可识别时报"不确定"。
- **错时超出支持明确拒绝**（`OUT_OF_SUPPORT_EVENT_TIME`），绝不外推。
- 处理反转、始终处理、控制污染都有独立失败类别；饱和设计下保留点估计、标注推断不可用。

## 2. 目录结构

```
app/
  contracts/models.py    统计契约：面板/请求/结果/失败类别（Pydantic 边界）
  core/
    alignment.py         对象身份对齐、权重冻结、2×2 处理路径分类
    inference.py         加权 OLS + 按对象聚类三明治（CRV1/CRV0）、Wald
    estimation.py        四格均值 DID、一阶差分回归、事件时间 TWFE、支持检查
    errors.py            带 FailureCategory 的估计错误
  evidence/diagnostics.py 平行趋势/污染/聚类数诊断（可证伪不可证明）
  reproducibility/        固定夹具（含手算答案）+ 指纹溯源
  data/ledger.py          SQLite 运行台账
  api/                    FastAPI 薄路由 + JSON 行结构化日志
  service.py              对齐→分类→估计→诊断 的编排
config/settings.yaml      本地配置（版本、库/日志路径、聚类设置、种子）
tests/                    独立测试 + 独立参考实现 + 手算答案
scripts/example_calls.py  不起服务的示例调用
METHODOLOGY.md            估计量与完整手算推导
```

## 3. 安装与锁定依赖

建议虚拟环境。关键依赖已在 `requirements.txt` 锁定（交付环境 Python 3.12.3 实测）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # 或：pip install -e ".[test]"
```

| 依赖 | 锁定版本 |
|------|----------|
| fastapi | 0.141.1 |
| uvicorn | 0.54.0 |
| pydantic | 2.13.5 |
| numpy | 2.4.6 |
| scipy | 1.15.3 |
| PyYAML | 6.0.1 |
| httpx / pytest / pytest-cov | 0.28.1 / 9.1.1 / 7.1.1 |

## 4. 运行测试（真实执行结果）

```bash
python3 -m pytest
```

交付时实测：**48 passed**，行/分支覆盖率 **94%**。测试断言具体数值与失败类别，
不是"接口能调用"：

- 四格均值、差分分解、DID 点估计与手算常量逐项相等；
- 聚类 se=√2（手算，见 `METHODOLOGY.md` §2.4）并与独立三明治实现一致；
- 缺期对象被排除且不补零、估计不变；
- 前置趋势不平行样例被**证伪**（d=3.0，p<0.05），平坦样例也只报"无法证明"；
- 处理污染被记录/拒绝，排除后干净 DID=4；
- 事件时间恢复 τ_0=5、τ_1=8，超支持请求返回 `OUT_OF_SUPPORT_EVENT_TIME`；
- HTTP 层验证状态码、信封、SQLite 台账与按 request_id 关联的日志。

## 5. 启动服务

```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8077
```

- 台账：`data/did_ledger.db`（SQLite 文件，自动建表）
- 日志：`logs/service.log`（JSON 行，每行带 request_id）
- 交互文档：http://127.0.0.1:8077/docs

## 6. 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/api/v1/health` | 健康与 core 版本 |
| GET  | `/api/v1/fixtures` | 合成夹具清单 |
| GET  | `/api/v1/fixtures/{name}` | 夹具观测 + 内容指纹溯源 |
| POST | `/api/v1/did` | 两组两期 DID |
| POST | `/api/v1/event-study` | 事件时间 TWFE |
| GET  | `/api/v1/runs/{request_id}` | 按请求身份查台账 |

### 示例：两组两期 DID

```bash
curl -s -X POST http://127.0.0.1:8077/api/v1/did \
  -H 'Content-Type: application/json' -d '{
  "request_id": "demo-1",
  "observations": [
    {"unit_id":"t1","period":0,"y":2.0,"treated":false},
    {"unit_id":"t1","period":1,"y":5.0,"treated":true},
    {"unit_id":"t2","period":0,"y":4.0,"treated":false},
    {"unit_id":"t2","period":1,"y":9.0,"treated":true},
    {"unit_id":"c1","period":0,"y":1.0,"treated":false},
    {"unit_id":"c1","period":1,"y":1.0,"treated":false},
    {"unit_id":"c2","period":0,"y":3.0,"treated":false},
    {"unit_id":"c2","period":1,"y":5.0,"treated":false}
  ]}'
```

返回（节选）：`estimate.value = 3`、`se = 1.41421`（G=4，dof=3），
`decomposition.did = 3`，并附四格均值、排除记录、诊断与逐步 trace。

### 示例：事件时间 + 超支持拒绝

```bash
# 窗口 -2..1：恢复 τ_0=5、τ_1=8，-1 归一化为 0
POST /api/v1/event-study  {"request_id":"e1","control_group":"never_treated",
  "min_event_time":-2,"max_event_time":1,"observations":[...staggered_events...]}

# 请求 k=2（无队列支持）→ HTTP 422
# {"status":"rejected","failure_category":"OUT_OF_SUPPORT_EVENT_TIME", ...}
```

不起服务也能看全部样例输出：

```bash
python3 scripts/example_calls.py
```

## 7. 请求字段（DID）

| 字段 | 默认 | 含义 |
|------|------|------|
| `request_id` | 必填 | 关联响应/台账/日志的身份 |
| `observations[].unit_id/period/y/treated/weight` | — | 稀疏单元；缺期即缺测，不补零 |
| `pre_period`,`post_period` | 最早/最晚 | 两期须都被观测且严格递增 |
| `balance` | `balanced` | `balanced`（仅两期都在）/ `unbalanced` |
| `weight_policy` | `unit_fixed` | 权重冻结基线；另有 `observation`/`none` |
| `control_group` | `never_treated` | 或 `not_yet_treated` |
| `reject_on_contamination` | `true` | 污染硬拒绝；false=排除并继续 |
| `alpha` | 0.05 | CI/检验显著性水平 |

## 8. 可解释性

- 每个响应都带 `request_id` 与 `core_version`；
- `steps` 给出 ingest→period_select→identity_align→classify_2x2→estimate 的
  入/出对象数；
- `excluded` 逐条列出被排除对象、失败类别与原因（含"未补零"说明）；
- **失败原因**（`failure_category`，rejected）与**不确定结论**
  （`diagnostics[].level=INDETERMINATE`，status 仍为 ok）严格分开；
- 同一 request_id 可在 `/api/v1/runs/{id}` 与 `logs/service.log` 回溯。

## 9. 剩余限制（如实说明）

- **交错 DID 的异质性处理效应偏误**：事件时间用经典 TWFE。在多期、多队列且处理
  效应随时间/队列异质时，TWFE 可能用到"坏比较"。当前实现做了支持检查与
  never/not-yet 控制区分，但**未实现** Callaway–Sant'Anna、Sun–Abraham 等稳健
  估计量；这是后续工作。
- **小样本推断**：CRV1 在 G 很小时仍可能过度拒绝；G<30 给 WARNING，G=2 且饱和时
  不报告标准误。生产级分析建议 CRV3 或 wild bootstrap（未实现）。
- **无鉴权/限流/TLS**：面向本地合成数据的单机服务，不应直接暴露公网。
- **权重模型**：提供基线固定/观测/等权三种解析权重，不含估计式倾向得分加权。
- 观测须为长面板（unit×period）；宽表需调用方先行整形。
