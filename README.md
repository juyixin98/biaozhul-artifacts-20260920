# TF 坐标树校验（离线机器人坐标变换服务）

纯后端 C++17 服务：维护一棵以父子坐标系为节点、以**带时间戳刚体变换**为边的
坐标树，拒绝**成环 / 多父冲突 / 无效四元数**；查询任意两坐标系时沿树路径
组合变换，在样本时间区间内对**位置线性插值、旋转走最短弧 SLERP**，
**禁止任何无限外推**。静态边与动态边分开管理，响应中逐条返回**实际使用的
样本时间**与**时间误差**。

- 语言/标准：C++17，仅 POSIX（Linux）
- 数学：Eigen 3.4.0（vendored，SHA256 锁定）
- 协议：自实现的最小 HTTP/1.1（keep-alive，一线程一连接）+ JSON
  （nlohmann/json v3.11.3，单头文件，SHA256 锁定）
- 密码学：自带 FIPS 180-4 **SHA-256 真实实现**，每个响应带
  `X-Content-SHA256` 头，客户端可独立复算（含 NIST 已知答案测试）
- 无任何系统级安装步骤，不需要 root，不依赖 apt 包

---

## 1. 目录结构

```
.
├── CMakeLists.txt
├── DEPS.lock                  # 依赖 URL + 锁定 SHA256
├── third_party/
│   ├── verify_deps.sh         # 强制校验依赖哈希
│   ├── eigen-3.4.0.tar.gz     # 解压为 eigen-3.4.0/
│   ├── eigen-3.4.0/
│   └── json.hpp               # nlohmann/json 单头文件
├── src/
│   ├── main.cpp               # 入口 / 信号处理 / SHA256 头注入
│   ├── api.cpp / api.hpp      # JSON API 与路由
│   ├── http_server.cpp/.hpp   # POSIX socket HTTP/1.1
│   ├── transform_tree.cpp/.hpp# 坐标树、校验、路径、插值、外推拒绝
│   ├── tf_math.hpp            # 四元数校验、逆、最短弧 SLERP
│   └── sha256.cpp/.hpp        # SHA-256
├── tests/test_main.cpp        # 76 项单元断言（与解析解核对）
├── scripts/
│   ├── build.sh               # 校验依赖 + 构建 + 单测
│   └── e2e_test.py            # 27 项端到端 HTTP 断言
└── examples/                  # 示例输入
```

---

## 2. 本地构建（依赖已随仓库锁定并就位）

需要 g++（支持 C++17，实测 g++ 13）与 cmake ≥ 3.16。**无需安装 Eigen**。

```bash
# 一键：校验依赖哈希 → 构建 → 单元测试
./scripts/build.sh

# 或手动：
./third_party/verify_deps.sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
./build/tf_tests
```

若重新拉取依赖（见 `DEPS.lock` 中的 URL），务必再跑 `verify_deps.sh`，
哈希不符会直接失败：

```
8586084f...2c1c72  eigen-3.4.0.tar.gz
9bea4c80...03ea6  json.hpp
```

---

## 3. 启动

```bash
./build/tf_server --host 127.0.0.1 --port 18080
# TF 坐标树校验服务监听于 http://127.0.0.1:18080
```
`Ctrl-C` / `SIGTERM` 优雅退出。

---

## 4. 验收命令（另开一个终端）

```bash
# 单元测试（无需起服务）
./build/tf_tests

# 端到端 HTTP 测试（默认连 18080；这里连 18081）
python3 scripts/e2e_test.py http://127.0.0.1:18081
```

手动冒烟：

```bash
B=http://127.0.0.1:18080

# 静态边 world -> mount（抬高 z=1）
curl -s -X POST $B/edges/static -H 'Content-Type: application/json' \
  -d @examples/01_static_mount.json

# 动态边 mount -> arm（5 个时间样本，位置沿 x、姿态绕 z）
curl -s -X POST $B/edges/dynamic -H 'Content-Type: application/json' \
  -d @examples/02_dynamic_arm.json

# 查询 arm -> world @ t=2.5（区间插值，逆方向经过两条边）
# -D - 同时打印响应头；响应体存到 /tmp/body.json
curl -s -D /tmp/hdr.txt -X POST $B/query -H 'Content-Type: application/json' \
  -d @examples/03_query_arm_to_world.json -o /tmp/body.json

# 用系统 sha256sum 独立核对响应完整性头（两者必须相同）
grep -i x-content-sha256 /tmp/hdr.txt
sha256sum /tmp/body.json

curl -s $B/frames | python3 -m json.tool
```

---

## 5. HTTP/JSON 协议

所有响应为 JSON，并带 `X-Content-SHA256: <hex>`（对**响应体逐字节**的
真实 SHA-256，可用 `sha256sum` 复算）。

四元数一律 `[w, x, y, z]`。

### `GET /health` → 200
### `GET /frames` → 200
返回每个坐标系的父节点、静态/动态、样本数与时间区间。

### `POST /edges/static`
```json
{
  "parent_frame": "world",
  "child_frame": "mount",
  "translation": [0.0, 0.0, 1.0],
  "rotation": [1.0, 0.0, 0.0, 0.0]
}
```
同一 `parent->child` 可覆盖；改变 child 的父节点 → `409 multiple_parent_conflict`；
会成环 → `409 cycle_detected`；零/NaN/Inf 四元数 → `400 invalid_quaternion`；
对同一 child 先静态后动态 → `409 static_dynamic_conflict`。

### `POST /edges/dynamic`
```json
{
  "parent_frame": "mount",
  "child_frame": "arm",
  "samples": [
    {"time": 0.0, "translation": [0,0,0], "rotation": [1,0,0,0]},
    {"time": 1.0, "translation": [2,0,0], "rotation": [0.9239,0,0,0.3827]}
  ]
}
```
样本可**异步、乱序、分批**到达：服务端按时间排序存储；相同时间戳后到样本
覆盖旧样本（重发安全）。内部用互斥锁保护，支持并发写入。

### `POST /query`
```json
{
  "source_frame": "arm",
  "target_frame": "world",
  "time": 2.5,
  "boundary_tolerance_seconds": 0.0
}
```
响应（节选）：
```json
{
  "translation": [1.9134, 4.6194, 1.0],
  "rotation": {"w": ..., "x": ..., "y": ..., "z": ...},
  "matrix": [[...4x4...]],
  "max_time_error": 0.5,
  "edges_used": [
    {"parent": "mount", "child": "arm",
     "direction": "child->parent (inverted)",
     "edge_type": "dynamic", "mode": "interpolated",
     "requested_time": 2.5,
     "sample_times_used": [2.0, 3.0],
     "time_error": 0.5},
    {"parent": "world", "child": "mount", "edge_type": "static",
     "mode": "static", "sample_times_used": [], "time_error": 0.0}
  ]
}
```

语义约定（ROS TF 同向）：边样本 `(t, p, q)` 表示**父→子**，
`p_child = R(q)·p_parent + p`。查询 `source -> target` 返回的矩阵把
**source 坐标下的点**变换到 **target 坐标**。路径由最近公共祖先（LCA）
拼接：source 向上的边取逆，target 向下的边正向相乘。

**插值**：位置 `lerp`；旋转为手写**最短弧 SLERP**（先把点积翻成非负，
恒定走低维夹角，接近重合时退化为归一化线性插值，避免转轴奇异）。
`mode` ∈ `exact | interpolated | static | clamped_earliest | clamped_latest`。

**禁止外推**：请求时间早于最早/晚于最晚样本 →
`422 extrapolation_forbidden`。可选 `boundary_tolerance_seconds`（默认 0）：
仅当与端点相差不超过该值时吸附到端点样本，并在 `mode`/`time_error` 中
如实标注——它不是外推，绝不延伸到区间之外。

`time_error`：插值时为到**最近**使用样本的距离；整条链取各边最大值。

### `POST /validate/quaternion`
返回 `valid / unit_length / norm`，非单位但可归一化时附 `normalized`。

### 错误码
| HTTP | error |
|---|---|
| 400 | `bad_request` / `invalid_quaternion` / `invalid_number` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `cycle_detected` / `multiple_parent_conflict` / `self_loop` / `static_dynamic_conflict` |
| 422 | `unknown_frame` / `disconnected_tree` / `extrapolation_forbidden` / `edge_without_data` |

---

## 6. 正确性如何验证

**单元测试 `tests/test_main.cpp`（76 断言，0 失败）**，全部与独立解析结果核对：

- **SHA-256**：NIST FIPS 180-2 向量 `""`、`"abc"`、448-bit 长串；增量更新一致性。
- **四元数校验**：零 / NaN / Inf 拒绝；模长 2 归一化。
- **逆变换**：`T·T⁻¹=I`，逆旋转=`Rᵀ`、逆平移=`−Rᵀt`，点往返。
- **链式组合**：world→a→b→c 与逐步矩阵积逐分量核对；c→world 等于积之逆；
  旁路 c→a（经 LCA）与 `(T_ab·T_bc)⁻¹` 核对。
- **插值**：中点位置与角度的解析值（如绕 z 中点把 x 轴转到
  `(1/√2,1/√2,0)`）；并与 Eigen 的 `slerp` 在多个 u 上交叉核对。
- **接近 180°**：179°/179.9°/179.99° 以及恰好 180°（含取负的等价表示），
  结果有限、单位长度、角度解析正确、与 Eigen 一致。
- **拒绝路径**：自环、环（静态与动态）、多父、静/动混用、未知帧、
  两棵不相连的树、向前/向后外推、超容差外推。
- **异步采样**：乱序插入、同时间戳覆盖（不增加样本数）、
  **8 线程 × 100 样本并发追加**（共 800，全部有序）后插值正确。

**端到端 `scripts/e2e_test.py`（27 断言）**：真实 HTTP 往返，Python 独立
实现四元数旋转与 4×4 矩阵乘法核对正反变换复合为单位阵，并对
`X-Content-SHA256` 用 `hashlib.sha256` 复算响应体。

实际运行结果（本机）：

```
==== 76 项断言，0 项失败 ====
==== e2e: 27 项断言，0 项失败 ====
X-Content-SHA256: 5bbed4ce...fa1f21   ==   sha256sum /tmp/body.json
```

---

## 7. 范围与非目标

- 纯后端，无前端页面；内存态、无持久化（重启清空，符合"离线校验"定位）。
- HTTP 为教学级最小实现（无 TLS / 压缩 / chunked 请求体）；请勿暴露到
  公网，定位为本地/内网离线工具。
- 时间戳为 `double` 秒，纪元任意；插值假设相邻样本间匀速/匀角速度。
