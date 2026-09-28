# 小型整数矩阵乘加（MAC）推理后端

非对称激活（per-tensor）+ 逐输出通道权重量化（per-channel）的小型整数矩阵乘加推理引擎，
带 FastAPI 服务、冻结校准工件、独立数值验证与可复现诊断。全部为本地合成数据，无外部账号依赖。

技术栈：Python 3.10+ / NumPy / FastAPI / pytest。

---

## 1. 量化语义（顺序是契约的一部分）

仿射非对称量化，实数与整数的关系：

```
real = scale * (q - zero_point)
```

一层整数 Linear 的完整运算顺序（见 `engine/kernels.py`、`engine/quantize.py`）：

1. **激活编码（逐张量）**：`qx = saturate(round_half_away(x / s_x) + zp_x)`
2. **权重（逐输出通道）**：校准期编码并冻结 `qw`、`s_w[o]`、`zp_w[o]`
3. **零点修正的整数乘加**（int64 工作区，再按声明位宽收窄）：
   ```
   acc[b,o] = Σ_k (qx[b,k] - zp_x) * (qw[o,k] - zp_w[o])
            = qx·qwᵀ − (Σqx)·zp_w − zp_x·(Σqw) + K·zp_x·zp_w
   ```
   全程整数，零浮点减法；展开项使零点修正顺序无歧义。
4. **bias 使用正确尺度**：bias 在**累加器尺度** `s_in · s_w[o]` 下量化
   `bq[o] = round_half_away(b[o] / (s_in · s_w[o]))`。其中 `s_in` 必须是
   **上游节点实际编码所用的尺度**（第 2 层即第 1 层的输出尺度），而不是输入尺度，
   也不是对隐藏激活重新估计的另一个尺度。
5. **缩放 → 舍入 → 加输出零点 → 饱和**：
   ```
   qo = saturate(round_half_away((acc + bq) · s_in·s_w[o]/s_out[o]) + zp_out[o])
   ```

- **舍入**：全局统一 round-half-away-from-zero（不使用 NumPy 的银行家舍入）。
- **累加位宽显式**：支持 `int32`（经典 int8 MAC）与 `int64`。执行前做**静态最坏情况**
  界检查，收窄前再做一次运行时检查；不依赖宿主语言整数回绕。放不下即 REJECT。
- **饱和显式**：裁剪发生在收窄前，逐元素饱和掩码会被上报，而不是静默截断。
- **ReLU 在整数域**：`q = max(q, zp)`，保证实数输出恰好夹到 0。
- **校准绑定模型版本**：所有 scale/zero_point 只在冻结时由固定校准集估计一次，写入
  工件并与 `model_version`（训练态内容校验和）、`quantizer_version`（舍入/零点语义版本）
  绑定。请求期**绝不**重新估计；超出校准范围的请求被拒绝。语义版本不符的工件拒绝加载。

## 2. 判定语义（ACCEPT / REJECT / INDETERMINATE）

每个错误都带有稳定的机器码、HTTP 状态、判定类别和 `request_id`：

| 判定 | 含义 | 触发示例 | code / HTTP |
|---|---|---|---|
| **ACCEPT** | 结果可用 | `/validate` 中所有非饱和元素都在解析误差界内 | 200 |
| **REJECT** | 确定性不安全/非法，调用方可修正 | 形状不符、非数值、超校准范围、累加器静态溢出、模型/版本不存在 | `invalid_input` 400、`input_out_of_calibration_range` 422、`accumulator_overflow` 422、`model_not_found` 404、`model_version_mismatch` 409、`invalid_quantization_parameters` 400 |
| **INDETERMINATE** | 已执行但正确性无法判定 | 观测误差超出解析界；**隐藏层饱和**（其裁剪误差不在界内） | `/validate` 返回 200 但 `decision=INDETERMINATE`；未预期内部故障 `graph_execution_failed` 500 |

最终层饱和是**设计内裁剪**：不计为越界，单独通过 `saturated_fraction` 报告。
隐藏层饱和会使逐层误差界失效，因此判 INDETERMINATE 而不是错误地签发 ACCEPT。

错误响应体：

```json
{ "error": { "code": "...", "category": "REJECT", "message": "...",
             "request_id": "req-...", "details": { "observed_max": 999.0,
             "calibrated_min": -1.99, "calibrated_max": 2.01, "...": "..." } } }
```

## 3. 模块职责

```
engine/
  tensor_types.py  量化张量/参数类型、dtype 界与显式饱和（不可变 dataclass）
  quantize.py      校准、编码/反编码、bias 尺度、逐通道重量化（含舍入顺序）
  kernels.py       显式整数 MAC、静态/运行时累加位宽检查、饱和掩码
  graph.py         计算图：LinearNode / ReLUNode / Graph，逐节点诊断
  model.py         训练态(TrainedModel) 与冻结量化工件(QuantizedModelArtifact)、版本绑定
  numerics.py      独立数值验证：float64 参考、纯 Python 大整数 oracle、解析误差界
  errors.py        带类别/HTTP/机器码的类型化错误
service/
  config.py        环境配置（本地默认）
  registry.py      工件只读注册表（启动加载，请求间共享，不做逐请求校准）
  inference.py     请求校验、图执行、ACCEPT/REJECT/INDETERMINATE 判定
  app.py           FastAPI 路由与统一错误信封
  logging_conf.py  带 request_id 的日志与脱敏
scripts/           构建演示模型 / 本地演示 / 启动服务
configs/           演示模型配置（固定随机种子）
tests/             独立组织的测试（unit / integration / api）
```

## 4. 复现步骤

```bash
# 1) 依赖（numpy / fastapi / uvicorn / httpx / pytest）
python3 -m pip install -r requirements.txt

# 2) 跑全部测试（会实际执行并报告结果）
python3 -m pytest tests/ -v

# 3) 由固定种子的合成“训练态”冻结量化工件（参数只在此刻估计一次）
python3 scripts/build_demo_model.py

# 4) 本地进程内演示：正常请求 ACCEPT、超范围 REJECT、与浮点参考比较
python3 scripts/demo_local.py

# 5) 启动 HTTP 服务
python3 scripts/serve.py --port 8000
```

服务冒烟：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/models
curl -s -X POST http://127.0.0.1:8000/models/demo_mlp/infer \
  -H 'Content-Type: application/json' -d '{"input":[[0.1,-0.2,0.3]]}'
# 超校准范围 -> REJECT 422，带回 observed/calibrated 关键状态与 request_id
curl -s -X POST http://127.0.0.1:8000/models/demo_mlp/infer \
  -H 'Content-Type: application/json' -d '{"input":[[999,0,0]]}'
# 与浮点参考比较，给出逐通道解析界与判定
curl -s -X POST http://127.0.0.1:8000/models/demo_mlp/validate \
  -H 'Content-Type: application/json' \
  -d '{"input":[[0.1,-0.2,0.3],[-0.5,0.5,0.0]]}'
```

## 5. 测试如何回答关键问题

- **手算逐元素整数结果**：`tests/test_exact_handcomputed.py` 使用完全手填的 2×2
  矩阵与手算期望（编码输入 `[12,9]`、零点修正 MAC `[13,-30]`、加 bias `[16,-32]`、
  输出整数 `[-1,-3]`、实数 `[2.0,-0.75]`），断言具体值。
- **参考答案独立于被测核心**：`engine/numerics.py` 的 `exact_integer_reference` 是
  纯 Python、任意精度整数的独立重实现，不导入 `kernels/graph/quantize`；测试用 AST
  断言它没有复用计算核心，并与内核逐元素一致。
- **极值 / 非零零点 / 逐通道不同尺度**：`tests/test_numeric_edges.py` 断言 int8 两端
  饱和、实数 0 在非零零点下精确往返、相同整数乘积在 5× 不同通道尺度下产生不同结果。
- **饱和与溢出不交给宿主语言**：断言精确饱和掩码，以及 int32 静态最坏情况溢出被
  REJECT（`accumulator_overflow`）、同一算子声明 int64 时可执行。
- **与浮点比较但不要求相等**：`tests/test_calibration_versioning.py` 断言存在非零量化
  误差、RMSE 有界，且每个非饱和元素满足由各舍入阶段导出的**逐通道解析误差界**。
- **失败类别是断言的一部分**：API 测试断言具体 HTTP 码、`code`、`category=REJECT`
  与 `request_id`，而不是“接口能调用”。
- **隐藏层饱和 → INDETERMINATE**：`tests/test_hidden_saturation.py` 用确定性手构模型
  验证隐藏层裁剪时不得签发 ACCEPT。
- **校准绑定版本**：断言冻结参数确定性、与模型版本绑定、篡改 `quantizer_version` 被拒。

## 6. 诊断与脱敏

失败日志与响应都带 `request_id`、节点名与关键状态（形状、观测/校准边界、位宽极限），
可据此复现“为何接受/拒绝/无法判定”。张量载荷永不落日志：`redact_context` 只放行
标量与短的整数/字符串列表，数组与嵌套结构替换为 `<redacted:type>` 标签
（见 `tests/test_redaction.py`）。

## 7. 配置

环境变量（均有本地默认）：`QENGINE_MODELS_DIR`、`QENGINE_HOST`、`QENGINE_PORT`、
`QENGINE_LOG_LEVEL`、`QENGINE_ENFORCE_CALIBRATION_RANGE`（默认开启范围拒绝）、
`QENGINE_MAX_BATCH`、`QENGINE_MAX_FEATURES`。演示模型参数见 `configs/demo_model.json`。
