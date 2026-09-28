# toeplitz-fft

Toeplitz 矩阵-向量乘法（matvec）与批量矩阵乘法（matmat）后端：循环嵌入 + FFT，
Python / FastAPI / NumPy / SciPy / mpmath。所有数据均为本地合成夹具，无外部依赖。

## 布局

```
toeplitz_fft/           # 核心包
  config.py             # 配置层（dtype、缓存、容差，可用 TOEPLITZ_* 环境变量覆盖）
  errors.py             # 类型化失败类别（category 字符串可被测试/客户端断言）
  embedding.py          # 循环嵌入：长度、共享元素校验、循环矩阵首列
  kernel.py             # FFT 计算内核（matvec / matmat，实数 rfft / 复数 fft）
  cache.py              # 计划缓存，键 = 完整尺寸 + 内核内容 SHA-256
  reference.py          # 独立参考：dense 直乘 + mpmath 50 位高精度
  evidence.py           # 误差度量、内存规模、带 run_id 的 JSONL 证据日志
  schemas.py            # pydantic 请求/响应模式（复数 = [re, im]）
  service.py            # FastAPI 接口
fixtures/generators.py  # 可复用合成夹具（确定性种子）
tests/                  # 独立测试层（pytest，46 项）
scripts/verify.py       # 端到端验证脚本，产出证据日志
requirements.txt        # 固定版本依赖
```

## 边界语义（重要）

- **Toeplitz 定义**：`T[i,j] = c[i-j]`（i≥j），`T[i,j] = r[j-i]`（i<j），
  `c` 为首列（长 m），`r` 为首行（长 n）。`T[0,0]` 为共享元素，
  **`c[0]` 与 `r[0]` 必须一致**（容差 `consistency_rtol/atol`，见 config），
  否则抛出 `InconsistentToeplitzError`（category=`inconsistent_shared_element`）。
- **嵌入长度**：`L = m + n - 1`（最小无循环混叠长度）。`pad_to_power_of_two=True`
  时向上取 2 的幂以加速 FFT，但**永不小于** `m + n - 1`，两种设置都无混叠。
- **模式**：`auto`（任一输入为复数即走复数路径）、`real`（rfft/irfft；
  复数输入仅当虚部全为 0 时接受，否则 `UnsupportedModeError`）、
  `complex`（强制 fft/ifft，输出复数）。
- **输出精度**：实数路径返回 `config.real_dtype`（默认 float64），
  复数路径返回 `config.complex_dtype`（默认 complex128），响应 meta 中明示。
- **批语义**：matmat 的 `X` 形状为 `(n, k)`，即 k 个列向量；HTTP 接口中
  `X` 为列向量列表，响应 `Y` 同为列向量列表（每个长 m）。
- **缓存**：键绑定 resolved mode、两种 dtype、嵌入长度 L 以及 c、r 规范化字节
  的 SHA-256；任一尺寸或内容变化都会 miss，不会复用陈旧计划。
- **极小问题**：包括 1×1 在内一律走嵌入 + FFT 路径，内核无任何 dense 捷径。
- **错误语义**：后端错误 → HTTP 400 + `error.category`；请求形状错误 →
  HTTP 422（pydantic）。异常不会被吞成成功。

## 运行

```bash
pip install -r requirements.txt
python -m pytest                      # 46 项测试
python scripts/verify.py              # 证据日志 → evidence/verify.jsonl
python scripts/verify.py --pad-pow2   # 2 的幂填充变体
uvicorn toeplitz_fft.service:app --port 8000
```

HTTP 示例：

```bash
curl -s localhost:8000/v1/toeplitz/matvec -H 'content-type: application/json' -d '{
  "c": [1.0, 2.0, 3.0], "r": [1.0, 4.0], "x": [5.0, 6.0]}'
# → {"y":[29.0,16.0,27.0], "meta":{"m":3,"n":2,"L":4,"mode":"real",...}}
```

复数在请求/响应中表示为 `[re, im]` 对。

## 证据

`scripts/verify.py` 对 12 个用例（非对称、非二次幂长度、复数、脉冲、批量、
手写小例、1×1）执行三路比对：FFT 内核 vs dense NumPy 直乘 vs mpmath 50 位
高精度（小尺寸），并报告 max_abs / max_rel / rel_l2 误差与内存规模
（FFT 工作区字节数 vs dense 矩阵字节数）。每条记录带 run_id、时间戳、
运行时版本（Python/NumPy/SciPy/mpmath）、输入 SHA-256 摘要与判定容差，
可按 run_id 追溯。脉冲用例的参考答案直接由 c、r 按下标规则构造，
不经过任何乘法；mpmath 参考与内核、BLAS 均不共享代码。

最近一次运行：12/12 通过，最大 rel_l2 误差 4.1e-16（float64/complex128）。

## 未执行 / 无法执行的检查（单列，未计入通过）

- **大规模性能基准**（如 m=n≥2^20 的耗时/峰值内存实测）：本环境未跑，
  内存规模仅以字节模型报告（`evidence.memory_report`），非实测 RSS。
- **float32/complex64 全量精度扫描**：仅测试了 dtype 切换正确性，
  未对低精度做系统误差统计。
- **并发压力测试**：服务层缓存为进程内 LRU，未做多 worker / 多线程竞争验证。
- **CI 流水线**：未配置持续集成；测试需本地手动触发。
