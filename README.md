# Toeplitz FFT Backend

Toeplitz 矩阵-向量乘 / 批量矩阵乘后端：用**循环嵌入 (circulant embedding) 与 FFT**
把稠密乘法降到 O(m log m)（`m >= 2n-1`），并提供**独立于被测内核**的数值证据。

- 技术栈：Python 3.12 · NumPy · SciPy (`scipy.fft`) · mpmath（高精度 oracle）· FastAPI
- 所有数据均为本地确定性合成夹具（固定种子 `20260927`），无外部账号/业务数据。

## 1. 目录结构（按职责分层）

```
src/toeplitz_fft/
  config.py       配置层：全部来自环境变量，不可变 Settings
  errors.py       显式失败分类（绝不把异常/未知状态折叠成成功）
  logging_ctx.py  run 身份 + 带 run_id 的结构化日志 + 版本块
  inputs.py       数值输入解析/校验（系统边界）：形状、有限性、共享元素、模式/精度
  kernels.py      计算内核：嵌入构造、稠密直乘（可解释路径）、rfft/fft 乘法、内存规模
  engine.py       计划/分派：路径选择 + 绑定“完整尺寸+系数摘要+内核摘要”的 LRU 缓存
  evidence.py     独立证据：mpmath 高精度 oracle、scipy 稠密 oracle、误差度量/判定
  fixtures.py     可复用合成夹具（非对称、非二次幂、复数非 Hermitian、脉冲、n=1）
  service.py      FastAPI：/health /metadata /compute /verify
scripts/verify.py 独立验证脚本（JSON 证据 + 带 run_id 的日志 + 显式退出码）
tests/            独立 pytest 套件（53 个测试，断言具体结果与失败类别）
reports/          已执行的证据产物（随仓库提供一次运行结果）
requirements.txt  固定版本依赖
```

不是单文件实现、不是调用壳、没有固定返回值：HTTP 层之下是完整的数值流水线。

## 2. 边界语义（嵌入与共享元素）

对 n×n Toeplitz `T`（首列 `c`、首行 `r`，且 `c[0] == r[0]`）构造长度 m 的
循环首列（**避免循环混叠**要求 `m >= 2n-1`，即线性卷积长度）：

```
v = [ c[0..n-1], 0...0, r[n-1], r[n-2], ..., r[1] ]
     |<- n 个 ->| 补零   |<------ n-1 个，放在向量末尾 ------>|
```

关键细节：反转的 `r` 段必须放在长度 m 向量的**末尾** `[m-n+1, m)`，这样循环
负下标回绕 `v[m-d] = r[d]` 才正确（`m == 2n-1` 时两段相邻，放错位置也碰巧对；
补零到快速 FFT 长度时放错就会得到错误的上三角元素——本仓库的脉冲测试专门钉住
这一点，开发中确实由此抓到过一个真实 bug）。共享元素 `c[0]=r[0]` 只使用一次；
二者不一致时边界直接拒绝（`first_element_mismatch`）。

随后 `y = ifft(fft(v) * fft(零填充 x))[:n]`。实数模式用 `rfft/irfft`，复数模式
用完整 `fft/ifft`；输出精度由请求显式指定：`double`(64 位) / `single`(32 位)。

## 3. 模式、精度与路径

| 请求字段 | 取值 | 含义 |
|---|---|---|
| `mode` | `real` / `complex` | 实数（rfft，输出浮点）/ 复数（输出 `[re, im]` 对） |
| `precision` | `double` / `single` | float64/complex128 或 float32/complex64 |
| `kernel` | `auto` / `embedding_fft` / `tiny_explicit` | `auto` 下 `n <= TOEPLITZ_TINY_N(默认 1)` 走可解释显式路径，其余走 FFT |

**极小问题**（n=1）默认走可解释正确路径（直接按定义构造矩阵相乘，不是固定答案）；
也可以用 `kernel="embedding_fft"` 强制 FFT，两条路径都有测试。

## 4. 缓存绑定语义

重复调用命中的缓存计划，其键绑定：路径、模式、精度、`n`、`batch`、嵌入长度 `m`、
**系数内容摘要**（同形状不同矩阵不共享）、以及**内核摘要**（内核名/版本/NumPy/
SciPy 版本）。命中时复用嵌入谱（每批只做向量自身的正变换）。tiny 显式路径没有
可复用谱，按设计不缓存。容量 LRU（`TOEPLITZ_CACHE_SIZE`，默认 128）。

## 5. 运行

```bash
pip install -r requirements.txt

# 单元/集成测试（TestClient 走完整 HTTP 栈，无需起服务）
python3 -m pytest tests/ -v
python3 -m pytest --cov=toeplitz_fft --cov-report=term-missing   # 覆盖率

# 独立证据脚本（退出码：0 全过 / 1 数值失败 / 2 装配错误）
python3 scripts/verify.py --report-file reports/evidence.json \
    --log-file reports/verify.log
TOEPLITZ_VERIFY_LARGE=1 python3 scripts/verify.py \
    --report-file reports/evidence_large.json \
    --log-file reports/verify_large.log   # n=4096 内存规模 + 64 点高精度抽样

# 服务
PYTHONPATH=src python3 -m uvicorn toeplitz_fft.service:app --port 8000
```

请求示例：

```json
POST /compute
{"first_column":[1,2,3],"first_row":[1,4,5],
 "vectors":[[1,0,-1]],"mode":"real","precision":"double","kernel":"auto"}
```

复数用 `[re, im]` 对，或 `{"real":[...],"imag":[...]}` 对象。`/verify` 在计算之外
附加独立 oracle 证据；数值不达标时返回 `ok:false` 且 HTTP 422，绝不冒充成功。

## 6. 证据（参考答案独立于被测内核）

- **mpmath oracle**：在高十进制精度（默认 80 dps）下按定义逐元素求和，纯 mpmath，
  不走 NumPy/SciPy FFT。大规模 n 用**确定性抽样探针**（必含首尾元素 + 固定种子
  随机点），报告里显式标注 `sampled:true` 与探针坐标。
- **scipy 稠密 oracle**：`scipy.linalg.toeplitz` 构阵 + BLAS 乘，另一条代码路径；
  并交叉核验两个 oracle 彼此一致（n ≤ 512 时）。
- 测试还包含**手算答案**（如
  `[[1,4,5],[2,1,4],[3,2,1]]·[1,0,-1]=[-4,-2,2]`）、**结构断言**
  （脉冲 `T e_k` 必须等于矩阵第 k 列），以及**破坏检测**（把答案改坏必须得到
  `numeric_accuracy` 分类）。

一次实际运行（`reports/evidence.json`，2026-09-28）：

| 夹具 | n | 路径/m | 对 mpmath 最大绝对误差 |
|---|---:|---|---:|
| 非对称实（非二次幂） | 13 | FFT / 25 | 7.1e-15 |
| 复数非 Hermitian | 17 | FFT / 33 | 5.4e-15 |
| 脉冲（结构断言） | 15 | FFT / 30 | 4.4e-16 |
| 非对称实 single | 31 | FFT / 63 | 6.7e-06（float32 门限内） |
| n=1 标量 | 1 | 显式 | 0 |
| n=1 强制 FFT | 1 | FFT / 1 | 0 |

大规模（`reports/evidence_large.json`，n=4096, batch=2, m=8192）：64 点高精度
探针最大绝对误差 **2.4e-13**；工作集约 **0.31 MiB**，稠密参考矩阵需
**128 MiB**，比值约 **682×**（脚本输出 `memory_saving_vs_dense_ratio`）。

判定依据（逐用例写入报告 `basis`）：
`max_abs <= tol_abs + tol_rel * scale`，double `tol_rel=1e-9, tol_abs=1e-10`；
single `1e-5 / 1e-6`。这些是工程门限，不是精度极限的声称。

## 7. 日志可关联性

每条日志带 `[run=<id>]`；HTTP 响应、证据报告、pytest 日志（`reports/pytest.log`，
每个测试一个 `test-<用例名>` run id）都能回溯到具体输入/运行。日志展示版本块、
进度（`progress=i/N`）、计算步骤（选路→构嵌入→复用/未命中→FFT→逐项核验）和
判定依据。

## 8. 失败分类

`shape_invalid, size_limit, empty_input, complex_spec_invalid, nonfinite_input,
first_element_mismatch, batch_length_mismatch, precision_unsupported, kernel_failed,
aliasing_unsafe, numeric_accuracy, bad_request, internal_error`。
畸形 JSON→400；输入类→422；数值证据失败→`/verify` 返回 422 且 `ok:false`；
未捕获异常→500 `internal_error`。

## 9. 配置（环境变量）

`TOEPLITZ_MAX_N`(65536) `TOEPLITZ_MAX_BATCH`(256) `TOEPLITZ_CACHE_SIZE`(128)
`TOEPLITZ_TINY_N`(1) `TOEPLITZ_ORACLE_PREC`(80 dps)
`TOEPLITZ_DENSE_ORACLE_MAX_N`(512) `TOEPLITZ_LOG_LEVEL`(INFO)。

## 10. 无法在本环境执行的检查（如实单列，未计为通过）

- **bandit 静态安全扫描**：环境未安装（`bandit: MISSING`），未运行。
  说明：服务不读文件、不执行命令、不接触密钥/SQL/HTML，攻击面限于 JSON 数值解析。
- **ruff / mypy**：未安装，未做静态检查；代码带完整类型注解，可在有工具的环境
  直接 `ruff check src tests`、`mypy src`。
- **black/isort 格式化**：未安装，未自动格式化（手工遵循 PEP 8）。
- **超大规模（n ≫ 4096）与 GPU 路径**：未验证；本实现为 CPU SciPy。
- 依赖固定版本在本机验证：Python 3.12.3 / numpy 2.4.6 / scipy 1.15.3 /
  mpmath 1.3.0 / fastapi 0.141.1 / pydantic 2.13.5 / uvicorn 0.54.0 /
  httpx 0.28.1 / pytest 9.1.1（coverage 7.16.1 仅用于度量）。
