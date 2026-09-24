# 关节轨迹时间参数化（Joint Trajectory Time Parameterization）

纯后端 C++ 离线服务：给定离散的**关节空间路径点**与每个关节的**速度、加速度上限**，
计算一条**严格递增时间**的时间参数化轨迹，输出各路径点的位置/速度/加速度状态，
并对**连续（段内）轨迹**做密集采样，逐点验证所有关节的速度与加速度约束。

- 语言/标准：C++17
- 数值：Eigen 3.4.0
- 协议：真实 HTTP/1.1（自实现的 POSIX socket 服务端，支持 keep-alive / 4xx / 413）
- 编解码：nlohmann/json 3.11.3
- 密码学：OpenSSL 3（EVP SHA-256，对请求原文做真实摘要）、`CRYPTO_memcmp`
- 无前端；无运行期联网下载，所有依赖用系统包并**锁定版本**。

---

## 1. 快速开始（本地启动）

依赖（Ubuntu 24.04 上已锁定并验证）：

| 依赖 | 锁定版本 | apt 包 |
|---|---|---|
| g++ | 13（实测 13.3.0） | `g++-13` |
| CMake | 3.28.3 | `cmake` |
| Eigen | 3.4.0 | `libeigen3-dev` |
| nlohmann/json | 3.11.3 | `nlohmann-json3-dev` |
| OpenSSL（libcrypto） | 3.0.13 | `libssl-dev` |

一键核对依赖版本（版本不符会直接失败，不做隐式替换）：

```bash
make verify-deps
# 或 bash scripts/verify_dependencies.sh
```

构建：

```bash
make build
# 等价于：cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
```

启动服务（默认只监听本机回环地址）：

```bash
make run                       # 127.0.0.1:8080
# 或：./build/jtp_server --host 127.0.0.1 --port 8080
```

健康检查：

```bash
curl -sS http://127.0.0.1:8080/health
# {"ok":true,"service":"joint-trajectory-time-parameterization","version":"1.0.0"}
```

---

## 2. 验收命令（测试）

```bash
make test                      # 单元测试 + HTTP 端到端测试
# 或分步：
ctest --test-dir build --output-on-failure
./build/test_parameterization  # 只跑单元测试（解析解/反向/短段/不可行边界/…）
```

端到端脚本会真实启动服务、用 `curl` 发 HTTP 请求、用 Python **独立重解整条
时间最优律并在物理时间上密集采样**（不信任服务端自己的验证结论），并打印
“限制生效的关节和区间”：

```bash
bash scripts/e2e_test.sh "$(pwd)/build/jtp_server" "$(pwd)/examples"
# 也可单独对任意 请求/响应 对做独立复核：
python3 scripts/independently_verify.py examples/request_multiaxis.json resp.json --samples 800
```

---

## 3. HTTP API

### `POST /parameterize`

请求（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `waypoints` | `number[][]`（n × dof） | 是 | 离散关节位置，n ≥ 2，维数一致 |
| `velocity_limits` | `number[]`（dof） | 是 | 每轴速度上限，严格为正 |
| `acceleration_limits` | `number[]`（dof） | 是 | 每轴加速度上限，严格为正 |
| `start_velocity` | `number[]`（dof） | 否 | 起点关节速度，缺省为 0；须与首段方向非负平行 |
| `end_velocity` | `number[]`（dof） | 否 | 终点关节速度，缺省为 0；须与末段方向非负平行 |
| `dwell_time` | `number`（秒） | 否 | 零长度段（重复点）分配的驻留时长，缺省 `1e-3` |
| `samples_per_segment` | `int ≥ 2` | 否 | 段内验证采样点数，缺省 201 |

示例：

```bash
curl -sS -X POST http://127.0.0.1:8080/parameterize \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request_multiaxis.json | jq
```

响应（节选）：

- `waypoints[]`：`time`（**严格递增**）、`position`、`velocity`、`acceleration`
- `segments[]`：每段
  - `duration`、`length`
  - `path_speed_ceiling` / `path_acceleration_ceiling`：路径坐标下的速度/加速度上限
  - `velocity_bottleneck_joint` / `acceleration_bottleneck_joint`：**哪个轴**的限制决定该上限
  - `binding_regime`：实际受限形态——`velocity`（达到速度上限的梯形）、
    `acceleration`（短段三角形，加速度受限）、`dwell`（零长度驻留）
  - `peak_path_speed`、瓶颈处利用率
- `zero_length_segments`：重复点/零长度段的原始下标
- `verification`：段内密集采样的验证结论（最大速度/加速度越界量、最坏关节、最小时间间隔、采样点数）
- `request_sha256`：对**收到的请求原始字节**用 OpenSSL EVP 计算的真实 SHA-256（小写 hex）

错误（HTTP 状态码 + JSON）：`400 INVALID_JSON`、`422 INVALID_WAYPOINTS /
INVALID_LIMITS / INVALID_PARAMETERS / INVALID_BOUNDARY_VELOCITY /
INFEASIBLE_BOUNDARY`、`404 NOT_FOUND`、`405 METHOD_NOT_ALLOWED`、
`411 LENGTH_REQUIRED`、`413 PAYLOAD_TOO_LARGE`、`431/500`。

### 其他

- `GET /health`：存活探针。
- 请求体上限 16 MiB（超限返回 413，不会尝试解析）。
- 支持 HTTP/1.1 keep-alive（e2e 第 8 步用同一条 TCP 连接连发两次请求验证）。

---

## 4. 算法（明确的插值方式与约束处理）

### 4.1 几何插值：关节空间分段线性（以弧长为参数）

对每段 k（q_k → q_{k+1}），令

- 段长（关节空间欧氏弧长） `L_k = ‖q_{k+1} − q_k‖₂`
- 单位方向 `u_k = (q_{k+1} − q_k)/L_k`
- 弧长参数 `s ∈ [0, L_k]`，位置 `q(s) = q_k + s·u_k`（**分段线性插值**）
- 时间律 `ṡ(s) = ds/dt`（沿路径的标量速率），`s̈ = d²s/dt²`

则关节速度 `q̇ = ṡ·u_k`，关节加速度 `q̈ = s̈·u_k`。

### 4.2 多轴瓶颈：整体时序让所有轴同时满足限制

只对**在该段运动的关节 j（|u_kj|>0）**取最严的一个（min）：

```
V_k = min_j v_max_j / |u_kj|      （路径速度上限；达到它的关节即速度瓶颈）
A_k = min_j a_max_j / |u_kj|      （路径加速度上限；达到它的关节即加速度瓶颈）
```

这样每一段在统一的路径时间律下，**所有运动轴**都同时满足各自上限；
报告中给出具体的瓶颈关节编号。轴在某段方向分量为 0 时在该段静止，
天然不构成该段瓶颈（短轴/不同轴瓶颈由此自动处理）。

### 4.3 时间最优速度曲线：bang–coast–bang / 三角形

对每个段在 `(s, ṡ)` 上解**双积分器最小时间**问题（加速度先 `+A_k`、
可选匀速 `V_k`、再 `−A_k`），给定边界路径速度 `s0, s1`
（内部路径点强制 `ṡ=0`，因为方向突变处非零速度需要无穷大加速度；
首末段可用 `start_velocity/end_velocity` 指定与方向平行的边界速度）：

- 无约束 bang-bang 峰值 `w* = sqrt(A_k·L_k + (s0² + s1²)/2)`
- 若 `w* < V_k`：**三角形**，`binding_regime="acceleration"`，加速度受限
  （短段情形，速度上限根本达不到）
- 否则：**梯形**，在 `V_k` 匀速巡航，`binding_regime="velocity"`，速度受限
- 这保证**不会出现“只按最大速度分段而忽略加速度”**：加速度始终是
  显式约束，巡航是否存在由加/减速距离决定。

**不可行边界**（如实返回 422，不静默放宽）：

- 请求的边界速度超过 `V_k` → `INFEASIBLE_BOUNDARY`
- 段长不足以在加速度限制内连接边界速度：
  `L_k < |s1² − s0²|/(2A_k)` → `INFEASIBLE_BOUNDARY`（短段刹不住/加速不到）

### 4.4 重复点与零长度段

连续重复点 `L_k = 0` 被保留为**驻留段**，时长 `dwell_time > 0`，
因此输出时间向量仍**严格递增**；这些段在 `zero_length_segments` 与每段
`zero_length/binding_regime="dwell"` 中明确报告。非连续重复（回到旧位置）
是正常运动（例如反向回到起点），照常处理。

### 4.5 段内采样验证

服务端对每段在物理时间上均匀取 `samples_per_segment` 个点（默认 201，
端点处可能存在的加速度跳变按段内相处理），用连续时间律计算 `q, q̇, q̈`，
统计对 `v_max / a_max` 的最大越界量（容差为上限量级的 1e-9，纯数值余量），
任何越界或非严格递增都会令 `verification.passed=false`。

此外 `scripts/independently_verify.py` 在服务端**之外**独立重推
V_k/A_k/瓶颈关节、重解时间最优律、重新密集采样，是验收用的第二来源，
不依赖服务端自检结果。

### 4.6 边界条件约定

- 内部路径点：完全停止（`ṡ=0`）。这是折线角点处物理可行的保守且
  逐段时间最优的选择；首末点可选非零平行速度。
- 输出 `acceleration` 为“离开该路径点的段加速度”（段首 +A 相）；
- 单元测试给出多个**解析真值**，例如 `0→10, v_max=2, a_max=1`：
  加/减速各 2 s、加速距离各 2、匀速 3 s、总时长 **T = 7 s**。

---

## 5. 测试覆盖

`tests/test_parameterization.cpp`（75 个断言，无外部依赖）：

1. **单轴解析例（梯形）**：T=7 s，速度与加速度上限都恰好达到。
2. **单轴解析例（三角形短段）**：`0→1, v_max=10, a_max=1`，峰值 1，T=2 s。
3. **多轴不同瓶颈**：各段瓶颈关节随方向切换；短段报告加速度受限。
4. **多路径点反向**：`0→10→0`，中点必须停止，反向加速度符号正确。
5. **重复点/零长度段**：驻留时长、严格递增时间。
6. **不可行短段**：2 m/s 起步但 0.5 m 内停不下来 → 422。
7. **不可行速度边界**：起步速度超过速度上限 → 422。
8. **不平行的边界速度** → 422。
9. **可行非零首末速度解析例**：s0=s1=1 时 T=5.5 s。
10. 输入校验（维数、非正上限、单点等）。
11. SHA-256 已知答案（空串、"abc" 的 FIPS 向量）、常数时间比较、JSON 往返。

`scripts/e2e_test.sh`（真实 HTTP）：健康检查、404/405/400/422/413、
keep-alive 单连接两请求、`request_sha256` 与 `sha256sum` 逐字节一致、
独立 Python 复核器（800 点/段密集采样）。

---

## 6. 密码学操作

- `src/crypto.cpp` 使用 **OpenSSL 3 EVP**（`EVP_DigestInit_ex/Update/Final_ex`，
  `EVP_sha256()`）对请求体计算真实 SHA-256；非自制/非桩实现。
- 摘要随响应返回并可与 `sha256sum` 比对（e2e 自动比对）。
- 另提供 `CRYPTO_memcmp` 常数时间比较，供需要比对摘要的调用方使用。

---

## 7. 目录结构与可复现构建

```
CMakeLists.txt                     构建（find_package 精确版本约束）
Makefile                           make build/test/run/verify-deps
Dockerfile.pinned                  ubuntu:24.04 + apt 版本锁定的可复现镜像
src/
  trajectory_time_parameterization.{hpp,cpp}   核心算法（Eigen）
  io_json.{hpp,cpp}                            JSON 协议
  crypto.{hpp,cpp}                             OpenSSL SHA-256
  http_server.{hpp,cpp}                        自实现 HTTP/1.1 服务端
  main.cpp                                     路由与服务入口
tests/test_parameterization.cpp    单元测试（解析真值）
scripts/
  verify_dependencies.sh           依赖版本锁定校验
  e2e_test.sh                      HTTP 端到端验收
  independently_verify.py          独立重解 + 密集采样复核
examples/                          示例请求（多轴/重复点/不可行/边界速度）
```

Docker（可选）：

```bash
docker build -f Dockerfile.pinned -t jtp:1.0 .
docker run --rm -p 8080:8080 jtp:1.0
```

所有计算（Eigen）、HTTP 交互（真实 socket）与密码学操作（真实 OpenSSL）
均实际执行；版本不符、构建失败或约束验证失败时脚本/程序均以非零状态码退出。
