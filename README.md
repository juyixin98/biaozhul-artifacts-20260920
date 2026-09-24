# 关节轨迹时间参数化服务 (Joint Trajectory Time Parameterization)

纯后端 C++ 服务。输入一条**离散多关节路径**（关节位置点序列）和每个轴的
速度、加速度上限，离线计算一条**时间最优**且让**所有关节同时**满足各自约束
的时间律，输出严格递增的时间戳和每个路径点的完整状态（位置 / 速度 / 加速度），
并给出**限制实际生效的关节与时间区间**、密集段内采样的约束验证结果，以及对
响应体的真实 SHA-256 校验和。

- 语言：C++17，矩阵库 Eigen 3.4.0（唯一第三方依赖，已锁定）
- 传输：自包含 HTTP/1.1（POSIX socket，无 Web 框架依赖）
- 数据格式：JSON（自带确定性解析 / 序列化器）
- 密码操作：自带 FIPS 180-4 SHA-256 实现（非桩，有标准测试向量）

---

## 1. 快速开始（本地构建与启动）

前置条件：g++/clang（支持 C++17）、CMake ≥ 3.16、pthread、Eigen 3.4.0
（系统已自带；缺失时构建脚本会自动下载并按 SHA-256 校验后内嵌到
`third_party/`）。

```bash
# 一键构建（自动检测 / 拉取依赖）
bash scripts/build.sh

# 启动 HTTP 服务（默认 127.0.0.1:8080）
./build/topp_server --host 127.0.0.1 --port 8080

# 另一终端：健康检查
curl -s http://127.0.0.1:8080/healthz

# 离线参数化
curl -s -X POST http://127.0.0.1:8080/parameterize \
     -H 'Content-Type: application/json' \
     --data @examples/request_basic.json
```

不启动服务也可直接用离线 CLI（便于脚本与批处理）：

```bash
./build/topp_cli examples/request_basic.json            # 文件
cat examples/request_basic.json | ./build/topp_cli -     # 标准输入
```

## 2. 一键验收（推荐先跑）

```bash
bash scripts/build.sh
python3 scripts/acceptance.py            # 含真实 HTTP 端到端；跳过 HTTP: --no-http
# 或仅跑 C++ 单元测试
(cd build && ctest --output-on-failure)
```

验收内容：单轴解析 bang-bang / 三角、多轴不同瓶颈共享同一时钟、反向运动、
短段数值、重复点、不可行边界、拐点 S′=0 反向、SHA-256 标准向量；对响应每个
状态独立复核（时间严格递增、有限、不越限）；用 Python 独立重算 SHA-256；
真实 socket 上的 200/404/422 状态码。

---

## 3. HTTP 协议

### `POST /parameterize`

请求字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `joint_names` | string[] 或给 `dof` | 关节名数组，长度 = 自由度数 |
| `points` | number[][] | `n` 个路径点，每个长度 = DOF；允许重复点 |
| `velocity_limits` | number[] | 每轴速度上限 `> 0` |
| `acceleration_limits` | number[] | 每轴加速度上限 `> 0` |
| `grid_cells` | int，可选 | 数值积分网格（默认 200，内部再细化） |
| `samples_per_cell` | int，可选 | 段内验证采样密度（默认 4） |
| `duplicate_tolerance` | number，可选 | 相邻重复点判定阈值（默认 1e-9） |
| `start_velocity` | number[]，可选 | 起点关节速度（默认静止；须与路径切向相容） |

响应（200）：

- `points[]`：每个**保留点**的 `{t, q, qd, qdd}`，`t` 严格递增，首项为 0。
- `duration`：总时长（秒）。
- `removed_duplicates` / `retained_input_indices`：折叠掉的连续重复点及其原索引。
- `active_intervals[]`：**哪一个关节、哪一种限制（velocity/acceleration）、
  在哪段路径坐标 `[u_from,u_to)` 与时间 `[t_from,t_to)` 生效**，附区间内最大
  利用率 `max_ratio`。
- `bottleneck_velocity_joint` / `bottleneck_acceleration_joint`：首要瓶颈轴。
- `verification`：密集段内采样验证结果（`passed`、最大速度 / 加速度利用率及
  对应关节、采样点数）。
- `method`：明确声明的插值与参数化方法。
- `checksum`：`"sha256:" + SHA256(其余字段的规范 JSON)`。

错误（JSON `{error,message}`）：

| 状态码 | error | 触发 |
|---|---|---|
| 400 | `BAD_REQUEST` | JSON 非法 / 形状不符 / 参数越界 |
| 422 | `INFEASIBLE_LIMITS` | 非正或非有限上限、去重后不足 2 点 |
| 422 | `INFEASIBLE_BOUNDARY` | 起点速度超过可行最大值或与路径切向不相容 |
| 422 | `INFEASIBLE_PROFILE` | 速度 / 加速度组合下路径不可行 |
| 404/405 | `BAD_REQUEST` | 路径不存在 / 方法不允许 |

CLI 的退出码与 HTTP 状态类别对应（0=2xx，4=4xx，5=5xx）。

---

## 4. 插值方式（明确声明）

采用**自然三次样条**（natural cubic spline）在路径坐标 `u∈[0,M-1]` 上对每个
关节独立插值 `S_j(u)`，节点等距 `h=1`，端点条件 `S''(0)=S''(M-1)=0`。
这保证了**位置、速度、加速度在节点处都连续（C²）**，因此“不能只按最大速度
分段”：跨段的加速度通过共同的路径时间律耦合。

在每段 `u∈[i,i+1]`（令 `x=u-i`，`d_i=S''(u_i)`）：

```
S(u)   = (1-x)q_i + x q_{i+1}
        + (((1-x)^3-(1-x)) d_i + (x^3-x) d_{i+1}) / 6
S'(u)  = q_{i+1}-q_i + (1-3(1-x)^2) d_i/6 + (3x^2-1) d_{i+1}/6
S''(u) = (1-x) d_i + x d_{i+1}
```

## 5. 时间参数化方法（为什么所有轴同时满足、且不漏掉加速度）

在路径 `q=S(u)` 上求时间律 `u(t)`，记 `sd = du/dt`、`sdd = d²u/dt²`：

```
qd_j  = S'_j(u) sd
qdd_j = S'_j(u) sdd + S''_j(u) sd²
```

整体流程（时间最优路径参数化，TOPP 思路）：

1. **去重**：折叠连续重复点（零长度段），记录原索引；不足两个不同点则报错。
2. **最大速度曲线 MVC**：对所有关节求 `sd² ≤ (vmax_j/|S'_j|)²`；并加入加速度
   多面体的两两交约束，以及关节切向经过零（拐点）时的几何界
   `sd² ≤ amax_j/|S''_j|`（这保证反向 / 急拐工况也可行）。
3. **反向 + 正向传播**：每个网格单元使用**单一共享的恒定切向加速度 sdd**，
   并在该单元的**多个内部子采样点**上对**每个关节**求
   `|S'_j·sdd + S''_j·sd²| ≤ amax_j` 的数值半空间交集，再与速度 MVC 取交。
   反向传播得到“从终点能制动到达”的可达曲线，正向传播在此之上尽快加速并
   被该曲线 / MVC 钳制。因为 sdd 是所有关节共用的一个标量，结果天然让
   **所有轴在同一时间轴上同时满足限制**，而不是各段、各轴各自取最大速度。
   段内多点约束也封死了“只保证格点、段内越界”的漏洞。
4. **段内采样验证**：对每个网格单元在开区间内多点重算全部关节的真实
   `qd/qdd` 并核对上下限；`verification.passed=false` 会如实报告越界量与关节。
5. 时间按恒定控制模型闭式积分，保证严格递增。

输出的 `active_intervals` 直接来自这些密集采样的利用率（≥0.99 视为饱和），
明确指出**限制生效的关节与区间**。

> 说明：单元控制 `sdd` 允许在网格节点跳变（时间最优 bang-bang 的标准特征），
> 因此验证在单元开区间内采样；端点用单侧极限。内部网格对用户网格再做固定
> 倍数细化以压低离散误差，密集验证的典型最大利用率在 1.0 的 ~0.1% 内。

---

## 6. 示例

- `examples/request_basic.json`：7 点 3 轴往返路径，各轴瓶颈不同。
- `examples/request_duplicates.json`：含连续重复点（被折叠）。
- `examples/request_start_velocity.json`：起点即达 vmax（无加速段）。
- `examples/request_infeasible_boundary.json`：起点超速，返回 422。
- `examples/response_basic.json`：basic 请求的一次实际响应（含 checksum）。

解析对照（单元测试中精确断言）：单段 0→10，vmax=2、amax=1 时
加速距离 `v²/2a=2`、加速时间 `v/a=2`、匀速 6 距离耗时 3，**最优总时长 T=7**；
多轴 `(0,0)→(10,5)`，`v=(3,1), a=(1,2)` 时速度瓶颈为轴 1、加速度瓶颈为
轴 0，共享时间轴下仍为 **T=7**。

---

## 7. 依赖与复现性

- `dependencies.lock`：锁定 Eigen 3.4.0 的官方 tarball URL 与 SHA-256。
- `scripts/setup_deps.sh`：下载后**先校验 SHA-256 再解包**到 `third_party/`。
- CMake 选择顺序：`-DTOPP_EIGEN_INCLUDE=...` → `third_party/eigen` → 系统包。
- 除 Eigen 外无第三方库；JSON、SHA-256、HTTP 均为仓内实现，便于审计。

仅用内嵌 Eigen 的离线构建：

```bash
cmake -S . -B build-vendor \
  -DCMAKE_DISABLE_FIND_PACKAGE_Eigen3=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-vendor -j
```

## 8. 目录结构

```
include/topp/   类型、样条、参数化、规划、JSON、SHA-256、HTTP、API 头文件
src/            对应实现 + server_main.cpp / cli_main.cpp
tests/          自包含单元测试（ctest）
examples/       示例请求 / 响应
scripts/        build.sh、setup_deps.sh、acceptance.py
dependencies.lock
```
