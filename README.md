# 自适应积分（adaptive_integration）

纯后端自适应数值定积分计算库与 JSON 接口。Python + NumPy 实现，核心求积
规则（Gauss-Kronrod 7-15、自适应 Simpson）、表达式解析器、误差预算分配
全部自行实现，不依赖任何数值求积第三方库。无前端、无网络服务。

适用规模：单变量、小到中规模积分（默认上限 100 万次函数求值、细分深度
≤100、初始子区间 ≤10 万）。不面向大规模/高维/长时间运行任务。

---

## 1. 目录结构

```
adaptive_integration/     计算库
  parser.py               安全表达式解析器（递归下降，无 eval）
  rules.py                Gauss-Kronrod 7-15 求积规则（QUADPACK 常数）
  core.py                 两个自适应驱动（全局预算 GK / 递归半预算 Simpson）
  config.py               容差、深度、输入范围的集中校验
  result.py               结构化结果与失败错误码
  api.py                  编程接口 integrate()
  io_layer.py             JSON 序列化
  cli.py                  stdin/文件 JSON 命令行接口
tests/                    pytest 自动化测试（157 项）
examples/                 请求样例 + 验收结果
scripts/acceptance.py     验收脚本（解析函数比对/高振荡/奇点/窄峰/失效展示）
```

## 2. 快速开始

需要 Python 3.10+ 与 NumPy（开发环境：Python 3.12.3 / NumPy 2.5.3）。

```bash
# 命令行：文件输入
python3 -m adaptive_integration.cli -f examples/request_basic.json

# 命令行：stdin
echo '{"expression":"x^2","a":0,"b":1}' \
  | python3 -m adaptive_integration.cli

# 编程调用
python3 - <<'PY'
from adaptive_integration import integrate
r = integrate("4/(1+x^2)", 0, 1)
print(r.status, r.value, r.error_estimate)   # converged 3.14159265... 5.2e-10
PY
```

## 3. JSON 接口

### 请求字段

| 字段 | 类型 | 必填 | 默认 | 范围 / 说明 |
|---|---|---|---|---|
| `expression` | string | 是 | — | 仅含变量 `x`，≤500 字符；支持 `+ - * / ^`、括号、常量 `pi/e` 与函数 `abs sqrt exp log ln log2 log10 sin cos tan asin acos atan sinh cosh tanh atan2`。幂运算优先于一元负号（`-x^2 = -(x^2)`），不支持隐式乘法 |
| `a`, `b` | number | 是 | — | 有限数，绝对值 ≤1e100；区间宽度 ∈ [1e-300, 1e100]。允许 `a>b`（结果翻号）与 `a==b`（结果为 0） |
| `method` | string | 否 | `gk15` | `gk15` 或 `simpson` |
| `abs_tol` | number | 否 | 1e-10 | [1e-13, 1]，绝对误差预算 |
| `rel_tol` | number | 否 | 1e-8 | [0, 1]，相对误差预算；全局目标 ε = abs_tol + rel_tol·\|I\| |
| `max_depth` | int | 否 | 60 | [1, 100]，单个子区间连续二分上限 |
| `max_evaluations` | int | 否 | 100000 | [15, 1_000_000]，函数求值硬上限 |
| `initial_intervals` | int | 否 | 1 | [1, 100000]，初始均匀子区间数 |
| `points` | number[] | 否 | — | ≤100 个严格位于 (a,b) 内的额外分点，不可重复（用于已知窄峰/奇点位置） |

### 成功响应（退出码 0）

```json
{
  "status": "converged",
  "method": "gk15",
  "result": 3.1415926535897936,
  "error_estimate": 5.155583041197922e-10,
  "evaluations": 15,
  "depth_reached": 0,
  "intervals": 1,
  "warnings": [],
  "diagnostics": {}
}
```

### 失败响应（退出码 1 = 积分失败，2 = 请求错误）

```json
{
  "status": "failed",
  "error_code": "INVALID_VALUE_AT_POINT",
  "error_message": "……（x=0）",
  "result": 0.0,
  "error_estimate": 0.0,
  "diagnostics": { "location": 0.0, "best_estimate_unreliable": 0.0 }
}
```

非有限值在 JSON 中序列化为字符串 `"NaN"/"Infinity"/"-Infinity"`，
输出始终是严格合法 JSON。

### 错误码（失败状态，不静默给值）

| `error_code` | 含义 |
|---|---|
| `INVALID_REQUEST` | 参数缺失/越界/类型错误（退出码 2） |
| `PARSE_ERROR` | 表达式无法解析或含未知符号（退出码 2） |
| `INVALID_VALUE_AT_POINT` | 求积节点上被积函数返回 inf/NaN（区间内或端点有奇点） |
| `DEPTH_LIMIT_REACHED` | 最差子区间达到 `max_depth` 仍不满足误差预算 |
| `EVALUATION_BUDGET_EXHAUSTED` | 达到求值次数硬上限仍不满足预算 |
| `ROUND_OFF_NO_PROGRESS` | 深度 ≥15 后细分不能再降低误差估计：浮点舍入平台或不可积奇点 |

失败时 `result`/`error_estimate` 若给出，只是当前最优近似并在
`diagnostics.best_estimate_unreliable` 中显式标注，不得当作积分值使用。

## 4. 算法与误差预算

### 4.1 Gauss-Kronrod 7-15（默认，推荐）

* [-1,1] 上 15 点 Kronrod 规则（多项式精度 21）嵌入 7 点 Gauss 规则
  （精度 13），节点/权值为 QUADPACK qk15 常数；节点全在开区间，端点
  奇异函数不会在端点被采样。
* 单区间误差估计：`|K15 − G7|`，并按 QUADPACK 方式用
  `∫|f − 区间均值|`（resasc）对绝对误差占优/振荡情形做保守抬高；
  再叠加舍入底噪 `50·eps·∫|f|`。
* 全局误差预算：维护以区间误差为键的大顶堆，**始终细分误差最大的
  子区间**；总误差估计降到 `abs_tol + rel_tol·|I|` 以下即判敛。
* 深度 ≥15 且细分后误差不下降 → `ROUND_OFF_NO_PROGRESS`。

### 4.2 自适应 Simpson

* 每区间用粗 Simpson `S1` 与两个半步面板 `S2`，误差估计
  `|S2−S1|/15`，接受值用 Richardson 外推 `S2 + (S2−S1)/15`。
* **误差预算递归对半分配**：区间二分时局部容差 `/2`；多个初始面板
  时全局预算先均分到面板。
* Simpson 采样等距节点且包含端点：端点奇异会立即触发
  `INVALID_VALUE_AT_POINT`（与 GK 的开节点策略形成互补）。

### 4.3 容差语义与可达到的精度

判敛目标是 `abs_tol + rel_tol·|当前近似|`。默认 1e-10/1e-8 对光滑
函数可稳定给出 1e-10 量级或更好的真实误差（见验收表“真实误差/误差
估计”比值，均 <1）。`abs_tol=1e-13` 且 `rel_tol=0` 会被直接拒绝：
该目标已处机器精度量级，无法可靠判敛。

## 5. 误差估计会失效的范围（重要，验收重点）

误差估计依赖“嵌套两级规则的差值”。当**两级规则同时漏看**被积函数
的结构时，差值为 0/极小，库会误报收敛。本库不隐藏这一固有盲区，
验收脚本 `scripts/acceptance.py` 对其逐一实测：

1. **窄峰漏检（GK 与 Simpson 共有）**：峰宽 δ 小于最粗面板节点间距，
   且峰心避开节点时，所有节点上 f≈0。实测 [0,1] 上归一化高斯峰
   `exp(-((x-0.37)/δ)^2)/(√π·δ)`（真值≈1）：δ=0.01 正确；δ≤0.003
   时单面板 GK15 仅用 15 次求值就“收敛”到 0，误差估计 4e-32 甚至 0，
   真实误差 1（过自信倍数达 1e31，见 `examples/acceptance_report.md`）。
   峰心恰在 0.5 时 δ=1e-4 同样漏检。
   **规避**：已知特征位置时用 `points` 包围（如 `[0.36, 0.38]`，
   435 次求值恢复到 2.5e-14），或增大 `initial_intervals`（2000 个
   初始子区间，3 万次求值恢复到 1.8e-13）。
2. **等距节点混叠（Simpson 特有）**：松容差下 sin(100x) 的粗面板两级
   Simpson 估计偶然一致，1009 次求值后返回 −0.1468（真值
   +0.001377），却报告 1.3e-9 误差（过自信约 1.1e8 倍）。
   **规避**：改用非等距节点的 `gk15`（675 次求值，真值误差 8e-17）；
   或收紧 `rel_tol`（=0 时 Simpson 正确但需 1.1 万次求值）；
   或增大 `initial_intervals`。频率继续升高（ω≥1000）时 Simpson
   在 10 万预算内转为**诚实失败**（`EVALUATION_BUDGET_EXHAUSTED`）。
3. **端点弱奇异（方法差异，非失效）**：`1/sqrt(x)`、`log(x)`、
   `x*log(x)` 在 x=0 可积。GK15 开节点可处理（1/sqrt(x) 在 1515 次
   求值、深度 50 下真实误差 1.4e-9，附端点深细分警告）；Simpson
   在端点直接采样，立即返回 `INVALID_VALUE_AT_POINT`。请用 gk15
   处理端点弱奇异，或先用变量代换消去奇性。
4. **真正不可积/内部极点**：`1/x`（[-1,1]）、`1/x^2`（[0,1]）必定
   失败——前者在节点 x=0 命中（两种方法都是 `INVALID_VALUE_AT_POINT`），
   后者 GK 在深度 16 触发 `ROUND_OFF_NO_PROGRESS`（细分后误差反而
   增大到 8.5e7），Simpson 端点采样即失败。任何路径都不会给出
   “有限积分值”。

## 6. 自动化测试

```bash
python3 -m pytest tests/ -q          # 157 个单元/端到端测试
python3 scripts/acceptance.py        # 验收：49 个数值场景，生成
                                     # examples/acceptance_{report.md,results.json}
```

测试覆盖：解析器优先级/错误表达式；qk15 常数对称性、权值和、
0–21 次多项式精度与嵌入 Gauss 0–13 次精度；8 个解析可查函数 ×
2 种方法的真实误差与估计关系；高振荡；内部极点、不可积奇点、端点
奇异；窄峰漏检与两种救援；深度/预算失败；参数校验；CLI 三种退出码；
严格 JSON 序列化（含 NaN 净化）。

## 7. 设计边界（不做什么）

* 不做多维积分、无穷区间（请先做变量代换）、复值被积函数。
* 不做被积函数缓存/并行（小规模无需；每次求值都计入预算）。
* 不对“请求容差比机器精度还严”的调用做静默放宽——直接拒绝。
* 不在误差估计失效时假装可靠：已知盲区见第 5 节，由调用方通过
  `points` / `initial_intervals` / 方法选择规避。
