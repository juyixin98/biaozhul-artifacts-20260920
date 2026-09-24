# TF 坐标树校验服务（tf_tree_server）

纯后端的离线机器人坐标变换服务。维护一棵由**静态边**和**动态边**组成的
坐标系森林：输入父子坐标系与带时间戳的刚体变换（平移 + 单位四元数），
在查询两个坐标系时沿树路径组合变换，并在时间区间内做插值。

- **语言/技术**：C++17、Eigen 3.4.0（几何运算）、自写 POSIX HTTP/1.1、
  OpenSSL 3 EVP_MAC（HMAC-SHA256）、nlohmann/json 3.11.3。
- **真实计算**：刚体变换的精确逆变换、沿路径链式组合、四元数最短弧 SLERP
  均由 Eigen 实际执行；鉴权由 OpenSSL 实际计算 HMAC-SHA256（含 RFC 4231
  已知答案测试）。无任何模拟/桩实现。

## 目录结构

```
.
├── CMakeLists.txt
├── src/
│   ├── tf_math.hpp       # Transform、compose/inverse、最短弧 SLERP、四元数校验
│   ├── frame_tree.hpp/.cpp  # 森林：环/多父拒绝、样本管理、路径组合、插值
│   ├── crypto.hpp/.cpp   # OpenSSL3 HMAC-SHA256、常量时间比较
│   ├── http_server.hpp/.cpp # 真实 HTTP/1.1（socket，逐连接线程）
│   ├── app.hpp/.cpp      # JSON 协议、鉴权、路由
│   └── main.cpp          # 入口、CLI、seed 文件加载
├── tests/
│   ├── cpp/test_frame_tree.cpp   # 解析解核对的单元测试（35 项断言）
│   └── http/test_http.py         # 真实 HTTP + 真实 HMAC 端到端测试（26 项）
├── examples/
│   ├── seed_static.json / seed_dynamic.json  # 可直接 --seed 加载
│   └── curl_demo.sh      # 带签名的 curl 全流程演示
├── scripts/fetch_deps.sh # 下载并按 SHA256 校验依赖
├── deps/SHA256SUMS       # 锁定的依赖哈希
└── third_party/          # fetch_deps.sh 解出的 Eigen 与 json 头文件
```

## 依赖（已锁定，vendored 到 third_party/）

| 依赖 | 版本 | 来源 | SHA256 |
|---|---|---|---|
| Eigen（纯头文件） | 3.4.0 | gitlab.com/libeigen 官方发布 tarball | `8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72` |
| nlohmann/json 单头 | v3.11.3 | GitHub release（与 raw.githubusercontent 双源比对一致） | `9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6` |

系统依赖（本机已具备）：g++（C++17）、CMake ≥ 3.16、OpenSSL 3 开发库、
pthread、Python 3（仅用于集成测试与演示脚本）。

依赖已随目录就绪；如需在干净检出中重新获取：

```bash
./scripts/fetch_deps.sh        # 下载并 sha256sum -c 校验
```

## 构建与本地启动

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)

# 带 HMAC 鉴权启动，并预载两个示例 seed：
./build/bin/tf_server --host 127.0.0.1 --port 8080 \
  --hmac-key demo-key \
  --seed examples/seed_static.json \
  --seed examples/seed_dynamic.json
```

其他参数：`--hmac-key-file PATH`（从文件读密钥）、`--no-auth`（显式关闭
鉴权，仅限本地调试）、`--timestamp-tolerance SEC`（默认 30s 防重放窗口）。
等价环境变量：`TF_HOST TF_PORT TF_HMAC_KEY TF_HMAC_KEY_FILE TF_NO_AUTH
TF_TIMESTAMP_TOLERANCE`。Ctrl-C / SIGTERM 优雅退出。

## 验收命令（一键）

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
cd build && ctest --output-on-failure && cd ..
# 35/35 C++ 断言 + 26 项 HTTP 端到端用例，应全部 Passed。
```

启动后做人工冒烟（另开一个终端）：

```bash
TF_PORT=8080 TF_HMAC_KEY=demo-key ./examples/curl_demo.sh
```

## HTTP 协议

基址 `http://127.0.0.1:8080`，请求/响应均为 JSON。

### 鉴权（HMAC-SHA256）

除 `GET /healthz` 外，所有请求都要带：

```
X-TF-Timestamp: <unix 秒>
X-TF-Signature: hex( HMAC_SHA256(key, METHOD\nRAW_TARGET\nTIMESTAMP\nRAW_BODY) )
```

- `RAW_TARGET` 为请求行中完整 target，**含 `?query`**（按原样，不重排参数）。
- `RAW_BODY` 为请求体原始字节；GET 为空串。
- 时间戳超出容差窗口 → `401 STALE_TIMESTAMP`；签名不匹配 →
  `401 BAD_SIGNATURE`（常量时间比较）。

### 端点

| 方法/路径 | 作用 |
|---|---|
| `GET  /healthz` | 健康检查（免鉴权） |
| `POST /v1/static` | 增加/重述静态边 `{parent,child,transform}` |
| `POST /v1/samples` | 增加动态边时间戳样本（批量，事务性） |
| `GET  /v1/query?from=A&to=B&time_us=T` | 指定时刻查询 `T_B_A` |
| `GET  /v1/query?from=A&to=B&latest=1` | 用各边最新样本组合 |
| `GET  /v1/tree` | 列出所有帧、父帧、边类型、样本时间窗 |

`transform` 形如：

```json
{"translation": [1.0, 0.0, 0.0],
 "rotation": {"w": 0.7071, "x": 0.0, "y": 0.0, "z": 0.7071}}
```

四元数为 `[w,x,y,z]`，模长须在 `1 ± 1e-3` 内（服务端归一化），拒绝 NaN/Inf、
零向量与非单位四元数。

### 查询响应（关键字段）

```json
{
  "from": "sensor", "to": "arm", "mode": "time",
  "query_time_us": 500000,
  "transform": {"translation": [...], "quaternion": {w,x,y,z}, "matrix": [[4x4]]},
  "path": ["sensor", "base", "arm"],
  "used_samples": [
    {"edge_child": "sensor", "type": "static", "interpolated": false, "queried_us": 500000},
    {"edge_child": "arm", "type": "dynamic", "interpolated": true,
     "sample_a": 1, "sample_b": 2,
     "stamp_a_us": 0, "stamp_b_us": 1000000, "alpha": 0.5, "queried_us": 500000}
  ],
  "sample_window": {"min_us": 0, "max_us": 1000000, "bounded": true,
                    "extrapolation": "forbidden"},
  "time_error_us": 0
}
```

- `transform` 是 **`T_to_from`**：把 `from` 坐标系中的点映射到 `to`。
- `used_samples` 逐边给出实际使用的样本：静态/动态、是否插值、括号样本 id、
  两端时间戳与插值系数 `alpha`。
- 指定时刻查询：插值恰好在请求时刻求值，时间残差 `time_error_us = 0`。
- `latest` 模式：取路径上每条动态边的最新样本；`time_error_us` 报告这些
  样本时间戳的**跨边最大跨度**（异步采样误差，单位 µs）。

## 坐标与校验语义

1. **森林结构**：每个子帧至多一个父帧。
   - 再认同一父帧：静态边允许重述（更新取值）；动态边允许继续加样本。
   - 试图给已有父帧的帧换父 → `409 MULTI_PARENT_CONFLICT`；
     静态/动态类型冲突同错。
2. **拒绝环**：新边 `parent→child` 若使 `child` 成为 `parent` 的祖先则
   `409 CYCLE_DETECTED`；自环同样拒绝。
3. **无效四元数**：分量非有限、零模或偏离单位模 → `422 INVALID_QUATERNION`。
4. **路径组合**：在无向树上 BFS 找路；沿下行方向的边直接取 `T_child_parent`，
   上行方向取其精确逆 `R^T, −R^T t`，再依次 `compose`。
5. **插值**：位置对相邻样本线性插值；旋转走**最短弧 SLERP**（点积为负时翻转
   四元数符号；近平行时退化为归一化 LERP，避免除零），已覆盖近 180° 情形。
6. **禁止外推**：查询时刻早于首样本或晚于末样本 → `422 OUT_OF_RANGE`；
   恰好等于样本时刻则直接用该样本（不插值）。
7. **异步采样**：样本可乱序、分批到达，服务端按时间戳归并；重复时间戳
   （批内或与已有）整批拒绝（`409 DUPLICATE_TIMESTAMP`，事务性，不留脏数据）。
8. **缺边/断连**：未知帧 `404 FRAME_NOT_FOUND`；两帧不在同一棵树
   `422 DISCONNECTED`。

## 与解析解核对的测试

- **逆变换**：`(Rz90, t=(1,0,0))` 的逆解析为 `(Rz−90, t=(0,1,0))`；
  任意刚体变换满足 `T·T⁻¹ = I`。
- **链式组合**：`world→a=(Rz90,(1,0,0))`、`a→b=(Rx90,(0,2,0))`，
  解析得 `T_b_w` 旋转矩阵 `[[0,-1,0],[0,0,-1],[1,0,0]]`、平移 `(1,2,0)`；
  逆查询平移 `(0,1,2)`。
- **动态中点**：`(I,(0,0,0))@0µs` 与 `(Rz90,(10,0,0))@10µs`，
  t=5µs 解析为 `(Rz45,(5,0,0))`。
- **近 180° 旋转**：179.999° / 恰好 180° 的 SLERP 中点及四元数反号情形。
- **HMAC-SHA256**：RFC 4231 用例 1、2 的标准已知答案。
- **HTTP 层**：鉴权（无签名/错签名/过期时间戳）、环、多父、无效四元数、
  插值、禁止外推、缺边、断连、异步样本、latest 时间跨度、坏 JSON、
  畸形 HTTP 请求、404。
