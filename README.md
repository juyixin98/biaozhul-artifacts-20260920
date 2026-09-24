# 栅格概率融合（Grid Probability Fusion）

纯后端的离线二维占据栅格融合服务：给定已定位的位姿（pose）和二维激光束
（beams），按 **log-odds** 增量更新占据概率。C++17 + Eigen + HTTP，无任何
前端/图形界面，结果以数值栅格（JSON / CSV）导出。

## 1. 更新规则（语义）

每束射线（起点为传感器，方向 = `pose.theta + beam.angle`）：

- **射线终点**（有返回束）：终点所在格 **occupied**，log-odds 加 `l_occ`；
- **沿途**：终点之前穿过的所有格 **free**，每格加 `l_free`；
- **传感器所在格**：束长 > 0 时记 free；束长 = 0 的返回束在本格记 occupied；
- **无返回束**（`"no_return": true`）：不产生任何 occupied 事件，按
  `max_range` 只更新沿途 free；
- **穿越地图边界**：只更新界内前缀（全部 free），界外格不触碰；返回束终点
  落在界外时同样只 free、不标记 occupied（响应里计入 `beams_clipped`）；
- **精确穿过格角**：确定性地走对角，两个仅在角点接触的侧邻格都不访问。

更新：`l ← clamp(l + Δ, l_min, l_max)`，因此概率有界、饱和；未知格
（从未观测）与“已观测但恰为 0.5”的格通过独立 `observed` 标志区分，导出时
未知格为 `null`（CSV 中为 `?`），而已观测的 0.5 是数值 `0.5`。

默认传感器模型：`l_occ=logit(0.7)=0.8473`，`l_free=logit(0.4)=-0.4055`，
`l_min=-2.0`（≈0.119），`l_max=3.5`（≈0.971）。后验
`p = 1/(1+exp(-l))`，用 Eigen 向量化计算。

## 2. 两套独立遍历与逐束参考比对

- 生产实现：`traceBeamAmanatidesWoo`（Amanatides–Woo 整数网格步进）；
- 逐束参考：`traceBeamSampled`（生成解析的跨边界事件表并按参数 t 排序，
  与 DDA 完全不同的实现），用于离线复核。

两者对同一批扫描必须逐格完全一致（`differing_cells == 0`）。这既是单元测试
（`test_grid`），也是 HTTP 接口 `POST .../verify` 和 CLI `verify` 命令。

## 3. 地图版本绑定（分辨率 + 原点）

版本 id 是对规范化几何字符串
`resolution|origin_x|origin_y|width|height`（double 用 17 位有效数字，保证
往返一致）计算的**真实 SHA-256**（本仓库自实现 FIPS 180-4，见
`src/sha256.cpp`，并有 NIST 已知答案测试）。

- 几何相同 → 同一版本 id（重复创建返回 200）；
- 改分辨率或原点 → 不同 id，光束绝不会混入不同几何的地图；
- 几何相同但传感器参数（`l_occ/l_free/...`）不同 → **409 Conflict**，
  防止参数变化混入旧格。

## 4. 构建（依赖锁定）

依赖：g++（C++17）、CMake ≥ 3.16、Eigen ≥ 3.3（系统包 `libeigen3-dev`）。
HTTP 与 JSON 为 **vendored 单头文件**，版本与 SHA-256 锁在
[`third_party/LOCK`](third_party/LOCK)，CMake 配置时强制校验，哈希不符直接
报错：

- cpp-httplib **v0.18.3**（`third_party/httplib.h`）
- nlohmann/json **v3.11.3**（`third_party/nlohmann/json.hpp`）

```bash
sudo apt-get install -y build-essential cmake libeigen3-dev   # 如未安装
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

## 5. 一键验收

```bash
./scripts/accept.sh                 # 构建 + 全部测试 + 真实 HTTP 端到端
PORT=18081 ./scripts/accept.sh      # 可选端口
```

或分步：

```bash
( cd build && ctest --output-on-failure )   # 3 个测试：SHA256 / 栅格规则 / HTTP
```

## 6. 本地启动 HTTP 服务

```bash
./build/gridfusion-server --host 127.0.0.1 --port 8080
```

### HTTP 协议（JSON）

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /health` | 健康检查 |
| `POST /api/maps` | 创建/复用地图，返回 `version` |
| `GET  /api/maps` | 列出地图 |
| `POST /api/maps/{version}/scans` | 灌入一帧扫描，更新 log-odds |
| `GET  /api/maps/{version}/grid[?logodds=1][?bbox=1]` | 导出数值栅格 JSON |
| `GET  /api/maps/{version}/grid.csv[?logodds=1][?bbox=1]` | 导出 CSV |
| `POST /api/maps/{version}/verify` | 与逐束参考实现比对（body 为扫描数组）|

创建地图（`examples/map.json`）：

```bash
curl -s -X POST localhost:8080/api/maps \
  -H 'Content-Type: application/json' --data @examples/map.json
# {"version":"9afb9434...","resolution":0.1,...}
```

灌扫描（字段：`pose{x,y,theta}`、`max_range`、`beams:[{angle,range,no_return?}]`；
`angle` 相对 `pose.theta`，弧度；`no_return` 束把 `range` 置 0 即可）：

```bash
V=$(./build/gridfusion version --map examples/map.json)
python3 - <<'PY'
import json,urllib.request
scans=json.load(open("examples/scans.json"))["scans"]
base="http://localhost:8080/api/maps/9afb9434b2ec1a927576896f7d432e95b7473984506c42003ef880f643bb916c"
for s in scans:
    req=urllib.request.Request(base+"/scans",data=json.dumps(s).encode(),
        headers={"Content-Type":"application/json"})
    print(urllib.request.urlopen(req).read().decode())
PY
```

导出（未知格为 `null`，已观测 0.5 为数值）：

```bash
curl -s "localhost:8080/api/maps/$V/grid?bbox=1" | head -c 400; echo
curl -s "localhost:8080/api/maps/$V/grid.csv?logodds=1" | head -3
```

参考比对：

```bash
python3 -c 'import json;print(json.dumps(json.load(open("examples/scans.json"))["scans"]))' > /tmp/all.json
curl -s -X POST "localhost:8080/api/maps/$V/verify" \
  -H 'Content-Type: application/json' --data @/tmp/all.json
# {"matches_reference":true,"differing_cells":0,"max_abs_logit_diff":0.0,...}
```

错误码：`400`（JSON/字段非法）、`404`（未知版本）、`409`（同几何不同传感器
参数）、`422`（位姿出界、range 超过 max_range 等语义错误）。

## 7. 离线 CLI（无需服务）

```bash
# 逐束参考实现比对（要求 differing=0，退出码非 0 表示失败）
./build/gridfusion verify  --map examples/map.json --scans examples/scans.json

# 融合并导出（--bbox 裁剪到已观测包围盒；--logodds；--csv；-o 文件或 stdout）
./build/gridfusion replay  --map examples/map.json --scans examples/scans.json \
    --bbox -o grid.json
./build/gridfusion replay  --map examples/map.json --scans examples/scans.json \
    --logodds --bbox --csv

# 计算地图版本 id（SHA-256）
./build/gridfusion version --map examples/map.json
```

`examples/scans.json` 由 `examples/gen_scans.py` 生成（仅用于生成示例数据，
服务本身不依赖 Python）：一个 5 m×5 m 房间、+x 墙有门洞（门洞方向为无返回
束）、中心扫描重复多次以体现饱和，另有一个偏移旋转视角。

## 8. 目录

```
include/gridfusion/   类型、栅格地图、射线、协议、服务头文件
src/                 实现（sha256 / raycast / grid_map / protocol / server / CLI）
tests/               test_sha256（NIST 向量）、test_grid（规则+参考比对）、
                     test_http（真实服务器+客户端端到端）
examples/            map.json、scans.json（合成房间）
third_party/         锁定版本与哈希的 httplib / nlohmann 单头 + LOCK
scripts/accept.sh    一键验收
```
