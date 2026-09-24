# 栅格概率融合 (Grid Probability Fusion)

纯后端的**离线二维占据栅格 log-odds 融合服务**。给定已定位的传感器位姿和激光束，
按确定性规则更新占据栅格的 log-odds；每次扫描形成一个**内容寻址、哈希成链**的不可变
地图版本；通过 HTTP/JSON 提供建图、融合、按版本导出和完整性校验。无前端页面。

- 语言：C++17（计算核心）+ Python 3（仅测试/验收客户端，标准库）
- 数值：Eigen 3.4.0（锁定）
- HTTP：cpp-httplib 0.15.3（锁定，明文 HTTP；鉴权在应用层用 HMAC 完成）
- JSON：nlohmann/json 3.11.3（锁定）
- 密码学：OpenSSL libcrypto（SHA-256 / HMAC-SHA256 / CSPRNG）

---

## 1. 融合规则（语义）

每个栅格维护一个 log-odds 值 `L`（初始 0，即先验概率 `p=0.5`），以及一个独立的
**是否已观测**标志。

| 更新类型 | 规则 | 增量 |
|---|---|---|
| 命中束（有返回） | 射线**沿途开区间**经过的格 | `L += l_free`（空闲） |
| 命中束 | **终点所在格**（且严格位于地图内部） | `L += l_hit`（占据） |
| 无返回束（超量程/无回波） | 起点到 `max_range` 沿束所有格 | 仅 `L += l_free`，**不产生占据** |

- 默认 `l_hit = logit(0.6) = ln(3/2)`，`l_free = logit(0.4) = ln(2/3)`，
  概率经 `p = sigmoid(L)` 得到。
- 每次更新后钳制 `L ∈ [-l_max, l_max]`（默认 `l_max=3`，约 0.047–0.953），
  **概率有界、会饱和**，重复扫描不会发散。
- **未知格 vs 已观测的 0.5**：从未被任何束碰到的格 `observed=0`，导出时
  `state="unknown", p=null`；被“先空闲后占据”抵消回 `L=0` 的格
  `observed=1, p=0.5`。二者严格区分。
- **穿越边界**：射线与地图矩形做 slab 求交，只有矩形内的段被更新；一条离开凸矩形
  的射线不会再回来。终点恰好落在地图边界线上时按“在外部”处理（只更新沿途空闲，
  不标记占据），避免边界归属二义性。
- **角点约定**：射线恰好穿过格角时同时跨入两个相邻格，对角旁侧的两个格不访问
  （Amanatides–Woo 确定性遍历；`tests/test_raycast.cpp` 有对角用例固化该约定）。
- 起点所在格属于开区间，按空闲更新；零长度命中束只把起点格标记为占据。

## 2. 地图版本与参数绑定

- 地图在创建时绑定 `width / height / resolution / origin / l_free / l_hit / l_max`。
- 扫描请求可用 `resolution` / `grid_origin` **回显**自己认为的栅格参数；与地图版本
  不一致时服务返回 **409 PARAM_MISMATCH**——参数变化必须新建地图，**不能把旧格混进
  新参数**。
- 每次扫描产生一个新版本（`seq` 从 0 的空版本开始递增）：
  - `payload_digest = SHA256(规范化扫描内容)`
  - `state_digest   = SHA256(规范化全图 log-odds + 观测掩码)`
  - `version_digest = SHA256("GRID-VERSION:v1" + seq + parent + payload + state)`
- 版本构成一条哈希链；`GET .../verify` 逐版本重放计算，任何格被篡改都会失配。
- 浮点规范化使用最短往返表示（C++ `std::to_chars` 与 Python `repr` 一致），log-odds
  在摘要中量化为 `floor(L*1e6 + 0.5)` 微单位，因此 C++ 与 Python 可对同一份内容
  算出相同摘要（见 `scripts/reference_check.py`）。
- 请求可带 `base_seq` 做乐观并发：客户端声明自己基于哪个版本，失配返回 409。

## 3. HTTP 协议

基址 `http://HOST:PORT`。除 `GET /health` 外所有接口都需要请求签名。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查（免鉴权） |
| POST | `/v1/maps` | 创建地图（201） |
| GET | `/v1/maps` | 列出地图 |
| GET | `/v1/maps/{id}` | 地图元数据 + 版本头 |
| POST | `/v1/maps/{id}/scans` | 应用一次扫描，返回新版本 |
| GET | `/v1/maps/{id}/grid?seq=N` | 导出数值栅格（默认最新，可按版本） |
| GET | `/v1/maps/{id}/versions` | 全部版本的摘要链 |
| GET | `/v1/maps/{id}/verify` | 重放校验版本链与状态 |

### 请求签名（HMAC-SHA256）

三个请求头：

```
X-PF-Timestamp: <unix 秒，与服务端 ±300s 内>
X-PF-Nonce:     <一次性随机串，服务端防重放>
X-PF-Signature: hex(HMAC_SHA256(secret, canonical))
```

`canonical` 为（最后带一个换行）：

```
SIGNED-REQUEST:v1
method=POST
target=/v1/maps/<id>/scans          # query 按字典序排序
timestamp=...
nonce=...
body_sha256=<请求体原始字节的 SHA-256，空体为 sha256("")>
```

错误代码：`MISSING_CREDENTIALS / BAD_SIGNATURE / TIMESTAMP_SKEW /
BAD_NONCE / REPLAY_DETECTED`（均 401）。签名覆盖**请求体哈希**，篡改一个字节即 401；
时间窗 + nonce 防止重放。

### 扫描请求格式

传感器形式（推荐，服务端用位姿换算世界坐标）：

```json
{
  "pose": [x, y, theta],
  "max_range": 5.0,
  "base_seq": 0,
  "resolution": 0.1,
  "grid_origin": [-2.0, -1.5],
  "returns": [
    {"angle": -0.2, "range": 2.55},
    {"angle":  0.0, "range": -1}
  ]
}
```

- `angle` 相对 `pose.theta`；世界端点 `o + range*(cos(theta+angle), sin(...))`。
- `range < 0` 或 `range >= max_range` 视为**无返回束**（只更新空闲）。

也可直接给世界坐标光束：

```json
{"pose": [0,0,0],
 "beams": [
   [ox, oy, ex, ey, true, 0.0],
   [ox, oy, dirx, diry, false, 4.0]
 ]}
```

### 导出示例（每个格）

```json
{"state": "observed", "p": 0.59868766, "log_odds": 0.40546510}
{"state": "unknown",  "p": null}
```

另含逐格累计 `free_touch_counts` / `occupied_touch_counts`、`state_digest`、
`version_digest`，全部为数值/JSON，无需任何图形界面。

---

## 4. 依赖锁定

构建不调用系统包管理器拉取三方库。`deps-lock.json` 与 `cmake/Dependencies.cmake`
锁定 URL + SHA-256，配置时先查项目内 `.deps-cache/`，否则下载并**强校验哈希**，
哈希不符直接中止。预置缓存已包含三个三方文件，可离线构建：

| 依赖 | 版本 | SHA-256（前缀） |
|---|---|---|
| Eigen | 3.4.0 tar.gz | `8586084f…1c72` |
| nlohmann/json | v3.11.3 json.hpp | `9bea4c80…3ea6` |
| cpp-httplib | v0.15.3 httplib.h | `a3347656…50ea` |
| OpenSSL | 系统 ≥3.0（libcrypto，需开发头文件） | — |

离线机器上保证 `.deps-cache/` 中三个文件存在即可（仓库已附带）。

---

## 5. 本地构建与启动

需要：g++/clang（C++17）、CMake ≥3.16、OpenSSL 3 开发包（`libssl-dev`）、
pthread、Python 3（仅验收脚本）。

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure      # 六个测试套件
```

生成密钥并启动（默认 127.0.0.1:8080，只监听本地回环）：

```bash
./build/pgrid gen-secret > secret.txt
./build/pgrid serve --host 127.0.0.1 --port 8080 --secret-file secret.txt
# 或：export PF_SECRET=$(./build/pgrid gen-secret)
#     ./build/pgrid serve --secret-from-env PF_SECRET
```

健康检查：`curl -s http://127.0.0.1:8080/health`

### 手工调用（用 Python 客户端签名）

```bash
export PF_SECRET=$(cat secret.txt)
MID=$(python3 scripts/pf_client.py create-map examples/create_map.json \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
python3 scripts/pf_client.py apply-scan "$MID" examples/scan_wall.json
python3 scripts/pf_client.py apply-scan "$MID" examples/scan_no_return.json
python3 scripts/pf_client.py export   "$MID" --seq 1 --out grid_v1.json
python3 scripts/pf_client.py verify   "$MID"
```

## 6. 一键验收

```bash
./scripts/accept.sh                 # 默认端口 18080；可用 PF_PORT=.. 覆盖
```

它依次：配置 → 构建 → C++ `ctest` → 启动服务 → 签名客户端跑通建图/扫描/导出/校验
→ 运行 `scripts/reference_check.py`（Python **独立**逐束重算，逐格比对 log-odds、
未知/已观测掩码、状态摘要和整条版本链）→ 关停服务。

## 7. 测试覆盖（场景对应需求）

| 测试 | 内容 |
|---|---|
| `test_math` | logit/sigmoid 互逆；上下饱和界 `±l_max` |
| `test_raycast` | 命中/无返回、穿越边界、边界终点、两侧打墙、对角角点、起点格；200 组随机束与密集采样参考逐格一致 |
| `test_fusion` | **批量扫描 == 逐束参考更新**（状态摘要相同）；重复扫描饱和；已观测 0.5 ≠ 未知；版本链重放；`base_seq` 冲突；非法几何拒绝 |
| `test_crypto` | SHA-256 NIST 向量；HMAC RFC 4231 向量；base64url 往返；CSPRNG；常量时间比较 |
| `test_http` | 健康检查；缺凭证/坏签名/时间偏移/nonce 重放；query 排序规范化；完整建图流程；body 篡改 401；参数不匹配 409；404 |
| `test_scenarios` | 墙面、穿越边界、重复扫描、无返回束四类场景，均与独立逐束参考逐格比对；按版本导出 |

## 8. 目录

```
cmake/        锁定依赖下载/哈希校验、httplib 编译单元
src/          类型、密码学、栅格/DDA、版本链、服务、协议、HTTP、CLI
tests/        六个 C++ 测试套件
scripts/      签名客户端、Python 逐束参考校验、一键验收
examples/     创建地图/墙面/无返回/原始光束 示例 JSON
deps-lock.json
.deps-cache/  已校验的离线依赖副本
```
