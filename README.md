# pgo-se2 — 二维 SE(2) 位姿图优化后端（纯后端）

使用 **C++17 + Ceres Solver + Eigen + SQLite + OpenSSL** 实现的二维位姿图优化
（pose graph optimization, PGO）。节点为 `(x, y, θ)`，边为相对位姿测量
`z = (Δx, Δy, Δθ)` 与 3×3 信息矩阵。输入图从本地 JSON 读取并**冻结版本**，
结果写入带 SHA-256/HMAC 签名的 JSON，每次运行记入 SQLite。无任何前端页面。

## 能力清单（对应作业要求）

- **SE(2) 残差**：`e = R(z)ᵀ ( R(from)ᵀ (t_to − t_from) − t_z )`，
  角度项 `Δθ` 用 `atan2(sin, cos)` 归一化到 `(−π, π]`，正确处理跨 ±π。
- **规范自由度**：用固定锚点消除。`single` 模式要求图连通（一个锚点）；
  `per_component` 模式给每个连通分量各固定一个锚点；不连通且要求单锚时
  **明确失败**（退出码 3），不产生结果文件。
- **信息矩阵**：逐边做特征分解，非对称 / 非正定 / 含非有限值一律拒绝
  （退出码 2）；SPD 但条件数 > 1e12 时告警但接受（病态权重验收）。
- **稳健损失**：支持 Huber / Cauchy，可全局或逐边设置；错误闭环被显著降权，
  好边保持低误差（见 `examples/bad_loop.json`）。
- **冻结输入版本**：`graph_version` 必填；对规范化（canonical）JSON 计算
  SHA-256 作为该输入图的内容指纹，同图重跑指纹恒定。
- **输出**：初始/末末加权代价、稳健代价、每条边在初始与最终位姿下的
  逐边误差（3 维残差、2-范数、加权平方、稳健代价）、锚点、连通分量数、
  Ceres 终止信息。
- **取消语义**：支持超时（`--cancel-after-ms`）与 `SIGTERM`/`SIGINT`。
  取消后运行记录进 SQLite（`cancelled`），但**绝不写出半优化结果**。
- **真实密码学**：OpenSSL EVP/HMAC 实现 SHA-256 与 HMAC-SHA256，
  结果体被篡改或密钥错误时 `verify` 失败。所有计算均真实执行。

## 目录结构

```
.
├── CMakeLists.txt          构建定义（锁定 find_package 版本下限）
├── DEPS.lock               依赖锁定（验证过的确切版本与允许区间）
├── src/                    实现
│   ├── types.h             数据模型 / 协议常量 pgo-input|result 1.0
│   ├── graph.{h,cpp}       JSON 加载、校验、SPD 检查、连通分量、选锚
│   ├── optimizer.{h,cpp}   Ceres 自动求导代价函数、稳健损失、可中断求解
│   ├── canonical.{h,cpp}   规范化 JSON（排序键、17 位 double）
│   ├── crypto.{h,cpp}      OpenSSL SHA-256 / HMAC-SHA256
│   ├── db.{h,cpp}          SQLite 运行日志（runs / edge_errors 表）
│   ├── report.{h,cpp}      结果报告签名与验证
│   ├── util.{h,cpp}        SE(2) 数学、角度归一化、信号、原子写文件
│   └── main.cpp            CLI: run / verify / generate / history
├── tests/test_unit.cpp     18 个单元测试（数学/校验/优化/DB/签名/取消）
├── scripts/
│   ├── check-env.sh        依 DEPS.lock 核查本地工具链（离线、不改动系统）
│   └── acceptance.sh       9 组端到端验收（28 个断言）
├── examples/               验收用冻结输入图
│   ├── drift_loop.json         带漂移闭环（3×2 矩形，严格几何一致）
│   ├── bad_loop.json           错误闭环（Huber 抵抗 vs 二次模型扭曲）
│   ├── disconnected.json       两个不连通等边三角形
│   ├── ill_conditioned.json    病态但正定权重（条件数 1e15）
│   ├── not_spd.json            负特征值信息矩阵（必须拒绝）
│   └── angle_wrap.json         朝向/测量跨 ±π
└── PROTOCOL.md             输入/输出协议、字段、密码与失败语义
```

## 一、本地启动

### 1. 依赖（Ubuntu 24.04 已验证，全部来自本地系统包，构建不联网）

```bash
sudo apt-get install -y \
  build-essential cmake g++ \
  libceres-dev libeigen3-dev libsqlite3-dev libssl-dev nlohmann-json3-dev
```

已验证版本见 `DEPS.lock`：g++ 13.3 / CMake 3.28 / Ceres 2.2.0（含
CHOLMOD 稀疏后端）/ Eigen 3.4.0 / SQLite 3.45.1 / OpenSSL 3.0.13 /
nlohmann-json 3.11.3。先核查环境（不满足直接拒绝构建）：

```bash
./scripts/check-env.sh
```

### 2. 配置与构建

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
# 产物：build/pgo（CLI）、build/pgo_tests（单元测试）
```

### 3. 一次优化运行

```bash
./build/pgo run \
  --input examples/drift_loop.json \
  --result /tmp/result.json \
  --db /tmp/runs.db
```

标准输出给出初始/末末代价、锚点、连通分量、迭代次数、结果哈希等；
完整逐边结果在 `--result` 文件。给结果加 HMAC 签名：

```bash
./build/pgo run --input examples/drift_loop.json \
  --result /tmp/signed.json --db /tmp/runs.db \
  --signing-key 'file:/etc/pgo/hmac.key'   # 也可直接给密钥或 env:VAR
./build/pgo verify --result /tmp/signed.json --signing-key 'file:/etc/pgo/hmac.key'
```

CLI 覆盖项（不改变冻结输入指纹，仅属本次运行控制）：
`--anchor-mode single|per_component`、`--loss huber|cauchy|none`、
`--loss-param`、`--max-iterations`、`--cancel-after-ms`、
`--sleep-before-ms`（取消测试钩子）。

## 二、验收命令（一键）

```bash
# 1) 环境
./scripts/check-env.sh

# 2) 单元测试（18 个：SE(2) 数学、SPD 拒绝、病态告警、断图选锚、
#    规范化 JSON、SHA-256/HMAC RFC 向量、收敛、错误闭环、取消、DB、签名）
ctest --test-dir build --output-on-failure

# 3) 端到端验收（28 个断言，自动生成 2000 节点大图做取消压测）
./scripts/acceptance.sh
```

`acceptance.sh` 覆盖：

1. **带漂移闭环**：代价 43.4 → ≈ 4e-25，闭环节点回到锚点，锚点不动；
2. **错误闭环**：Huber 下好边加权平方和 ≈ 0.02、错误边 ≈ 439 但稳健代价
   仅 ≈ 2.1；关闭稳健损失后好边被扭曲到 ≈ 99.6（稳健收益量化可见）；
3. **断图**：`per_component` 两分量各自锚定并收敛到 ~1e-17；
   `single` 明确失败（退出码 3、无结果文件、错误码 `graph.disconnected`）；
4. **病态权重**：条件数 1e15 仍正定，告警且求解完成；
5. **非正定信息**：退出码 2、无结果文件、原因写入 stderr 与 SQLite；
6. **跨 ±π 角度**：所有残差/角度落在 `(−π, π]` 且收敛到 ~1e-6 以下；
7. **哈希与签名**：HMAC 正/误密钥、结果体篡改检测、未签名 SHA-256 校验、
   冻结输入指纹确定性；
8. **取消**：超时与 SIGTERM 均退出码 4、无结果文件、SQLite 记 `cancelled`；
9. **逐边误差**：初始与最终每条边齐备，顺序与输入一致，总量单调下降。

## 三、退出码契约

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 命令行用法错误 |
| 2 | 输入非法（含非正定/非对称信息矩阵、模式/字段错误） |
| 3 | 图不连通且锚定策略无法处理（`single` 遇到多分量） |
| 4 | 被取消（超时或信号），**不发布结果** |
| 5 | 求解器未收敛，如实失败，不发布结果 |
| 6 | 结果校验失败（哈希/HMAC 不符、文件损坏） |
| 7 | IO 或数据库错误 |

## 四、结果完整性与失败诚实性

- 结果文件用“同目录临时文件 + fsync + rename”原子发布；取消/失败路径
  不会留下半优化结果。
- 结果文档为 `{protocol, body, manifest}`；`manifest.body_sha256` 是对
  规范化 body 的 SHA-256，可选 `hmac_sha256 = HMAC(key, body_sha256)`。
  `verify` 重算两者，任何字节级改动都会被发现。
- 求解失败、非正定、断图、取消等均如实返回非零码并记录原因，绝不伪造成功。

协议字段细节见 [`PROTOCOL.md`](PROTOCOL.md)。
