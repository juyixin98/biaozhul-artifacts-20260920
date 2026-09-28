# sumcompare — 大流式数列求和比较后端

对同一输入流并行执行 **朴素（naive）/ 配对（pairwise）/ 补偿（Kahan-Neumaier）** 三种求和，
以 mpmath 高精度结果为参考，给出可解释误差（实测误差 + 理论界 + 条件数），
并通过重排探针展示何种重排影响何种方法。

## 结构

```
app/
  kernels/            # 计算内核（真实职责：只做求和）
    policy.py         #   固定的 NaN/Inf/带符号零规则 + 输入扫描（脱敏画像）
    naive.py          #   朴素：顺序累加，分块后块和再顺序累加
    pairwise.py       #   配对：块内递归两两合并（基线块用 numpy），块和再两两合并
    compensated.py    #   补偿：Neumaier 累加器；分块合并保留 CompensatedState(total, compensation)
  reference.py        # mpmath 高精度参考和（默认 50 位十进制）
  error_analysis.py   # 误差证据：实测误差、一阶理论界、条件数
  inputgen.py         # 合成输入：大数相消 / 小数累积 / 随机宽幅；分块与块序重排
  diagnostics.py      # 请求标识 + 脱敏结构化日志
  models.py           # pydantic 请求/响应模型
  service.py          # FastAPI 接口
  config.py           # 配置加载（YAML 默认 + SUMCMP_* 环境变量）
config/default.yaml   # 声明式默认配置
tests/                # 独立测试（精确参考用 fractions.Fraction，不靠被测核心自证）
scripts/example_calls.sh
```

关键设计：**补偿分块合并不是"各块求和再相加"**。每块产出
`CompensatedState(total, compensation)`，合并用一次 Neumaier 步把对方 `total` 并入，
同时把双方累计的补偿一起携带（`app/kernels/compensated.py::merge`）。
`tests/test_chunked_merge.py` 证明假合并（只加块和）得 512，真合并精确得 1000。

## 固定特殊值规则

| 情形 | 行为 |
|---|---|
| 空输入 | 422 `empty_input` |
| 含 NaN | 422 `non_finite_input`（边界拒绝，不静默传播） |
| 同时含 +Inf 与 -Inf | 422 `mixed_infinities`（不定式） |
| 仅单一符号 Inf | 200，结果为该符号 Inf |
| 带符号零 | 结果为 -0.0 当且仅当所有输入项均为 -0.0；其余精确零结果一律 +0.0 |
| 数组超 `max_values` | 422 `input_too_large`（改用 generator 声明式输入） |

规则同时由 `GET /v1/policies` 对外公布。

## 误差理论（float64，eps = 2^-52）

- 朴素法：`|E| <= (n-1) * eps * sum|x|` —— 随 n 线性增长
- 配对法：`|E| <= ceil(log2 n) * eps * sum|x|` —— 随 n 对数增长
- 补偿法：`|E| <= (2*eps + n*eps^2) * sum|x|` —— Kahan 经典界，近似与 n 无关
- 条件数 `kappa = sum|x| / |sum x|` 解释输入难度：大数相消时 kappa 巨大

每个响应的 `error` 字段给出实测误差（对 mpmath 参考）、理论界与 `within_bound` 判定。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 关键依赖已锁定
.venv/bin/python -m pytest                  # 50 个独立测试
.venv/bin/python -m app.main                # 监听 127.0.0.1:8429（见 config/default.yaml）
```

## 示例调用（已真实执行验证）

```bash
# 1) 经典相消 [1e16, 1, -1e16]：朴素/配对得 0，补偿得 1（真值 1）
curl -X POST localhost:8429/v1/compare -H 'content-type: application/json' \
  -d '{"values": [1e16, 1.0, -1e16], "chunk_size": 2}'

# 2) 大流式小数累积：0.1 x 1,000,000（generator 声明，不传数组）
curl -X POST localhost:8429/v1/compare -H 'content-type: application/json' \
  -d '{"generator": {"kind": "small_accumulation", "count": 1000000, "value": 0.1}, "chunk_size": 8192}'

# 3) 重排探针：12 次随机块序重排，看各方法结果散布
curl -X POST localhost:8429/v1/compare -H 'content-type: application/json' \
  -d '{"generator": {"kind": "random_spread", "n": 4096, "seed": 11}, "chunk_size": 64, "reorder_trials": 12}'

# 4) 单方法求和（带误差报告）
curl -X POST localhost:8429/v1/sum -H 'content-type: application/json' \
  -d '{"values": [1e16, 1.0, -1e16], "method": "compensated", "chunk_size": 2}'
```

实测输出（本机，Python 3.12）：

```
用例 1 [1e16, 1, -1e16]   naive=0.0  pairwise=0.0  compensated=1.0  (ref=1.0, kappa=2e16)
用例 2 0.1 x 1,000,000    naive abs_err=1.43e-08 (bound 2.2e-05)
                          pairwise abs_err=5.6e-12 (bound 4.4e-10)
                          compensated abs_err=5.6e-12 (bound 4.4e-11)
用例 3 重排探针 12 次      naive:    9 个不同结果, spread=3.4e-04
                          pairwise: 7 个不同结果, spread=1.2e-04
                          compensated: 1 个结果,   spread=0  ← 对块序重排免疫
```

结论：块序重排显著影响朴素法，次之影响配对法（块和仍按块序合并），
补偿法因合并保留补偿状态而对该类重排基本免疫。

## 诊断与脱敏

- 每个请求分配 `request_id`（或沿用 `X-Request-ID` 头），贯穿响应体、响应头与日志；
- 日志只记录脱敏画像：计数、零/无穷计数、最大绝对值、输入字节流的 sha256 摘要（前 16 位）——
  绝不打印原始数值序列；
- 拒绝时响应体携带稳定失败类别（`category`）与计数级细节，说明为什么拒绝。

示例日志行：

```
request_id=demo-nan-001 decision=rejected category='non_finite_input' nan_count=1 digest='6bf4787bffa40bac'
request_id=edf19307b047 decision=accepted methods='naive,pairwise,compensated' chunk_size=64 count=4096 ...
```

## 测试组织（tests/）

- `test_kernels.py` — 硬编码期望值 + `fractions.Fraction` 独立精确参考
- `test_chunked_merge.py` — 证明分块合并必须保留补偿状态（假合并 512 ≠ 真合并 1000）
- `test_special_values.py` — NaN/Inf/带符号零/空输入的具体失败类别
- `test_error_bounds.py` — 实测误差 ≤ 理论界；补偿法在相消/累积输入上显著优于朴素法
- `test_reorder.py` — 重排敏感性：朴素法结果随块序改变，补偿法散布 < 1e-12 相对
- `test_reference.py` — mpmath 参考自身与 Fraction 及已知常数（巴塞尔问题）交叉验证
- `test_api.py` — 接口级具体结果断言、失败类别、请求标识回显

参考答案来源：`fractions.Fraction` 精确有理数、硬编码字面量、已知数学常数——
不由被测核心实现生成。

## 剩余限制

- mpmath 参考为纯 Python 循环：1e6 项约 30 s。更大输入建议调小 `reference_dps`
  或关闭单点误差报告（`with_error_report: false`）；内核本身（朴素/配对/补偿）为 O(n) 且很快。
- 理论界是一阶经典界（忽略 O(eps^2) 以上项，补偿法界含 n*eps^2 项），不是严格包络；
  测试用实测误差对界做了验证，但极端构造输入下界可能不严格成立。
- 补偿合并的块序免疫是"实测散布为零/极小"，不保证任意重排逐位一致（本就不要求）。
- 输入一次性物化为 list（generator 也是先物化再算）；真正的增量流式（边收边算）
  内核已支持（`CompensatedState` 不可变、可逐步 `add_term`），但 HTTP 层未暴露流式端点。
- 仅 float64；复数、float32、十进制输入未支持。
