# 位姿图优化后端（2D SE(2) Pose Graph Optimization）

纯后端的二维 **SE(2)** 位姿图优化器：节点与相对位姿边来自本地 JSON，固定
锚点消除规范自由度（gauge freedom），使用 **Ceres** 求解、**SQLite** 做冻结
版本与运行审计。无前端页面。

- 语言/标准：C++17
- 求解器：Ceres Solver 2.x（自动求导，SPARSE_NORMAL_CHOLESKY / DENSE_QR）
- 线性代数：Eigen 3.3+（Cholesky 校验信息矩阵）
- 存储：SQLite 3.31+（冻结图、运行记录、逐边误差、优化后位姿）
- 哈希：自带 FIPS 180-4 SHA-256 实现（用标准已知答案测试验证）

---

## 1. 目录结构

```
.
├── CMakeLists.txt          构建（强制依赖最低版本）
├── deps.lock               锁定依赖版本（scripts/lock_deps.sh 生成）
├── src/                    核心库 + CLI
│   ├── json.{hpp,cpp}      零依赖 RFC 8259 JSON 解析/写入
│   ├── sha256.{hpp,cpp}    SHA-256
│   ├── se2.hpp             SE(2) 复合、求逆、角度包裹（支持 Ceres Jet）
│   ├── graph.{hpp,cpp}     图模型、JSON 加载与严格校验、连通分量
│   ├── optimizer.{hpp,cpp} Ceres 残差、稳健损失、锚点、取消回调
│   ├── db.{hpp,cpp}        SQLite 迁移/冻结/运行审计
│   ├── io.{hpp,cpp}        规范化哈希、结果 JSON、原子写
│   ├── signal.{hpp,cpp}    SIGINT/SIGTERM 协作式取消
│   └── main.cpp            pgo CLI
├── tools/
│   ├── pgo_gen.cpp         验收用图生成器
│   └── pgo_sql.cpp         只读 SQLite 查询工具
├── tests/
│   ├── test_main.cpp       单元测试（51 项检查）
│   └── acceptance.sh       端到端验收（33 项断言）
├── examples/graph_example.json
└── scripts/lock_deps.sh
```

---

## 2. 本地启动

### 2.1 安装依赖（Ubuntu 24.04）

```bash
sudo apt-get install -y \
  build-essential cmake \
  libceres-dev libeigen3-dev libsqlite3-dev libsuitesparse-dev
```

> 已锁定并验证的版本见 `deps.lock`（Ceres 2.2.0 / Eigen 3.4.0 /
> SQLite 3.45.1 / SuiteSparse 7.6.1，g++ 13.3，CMake 3.28）。
> CMake 在 configure 阶段强制 `Ceres>=2.1`、`Eigen3>=3.3`、`SQLite3>=3.31`。

### 2.2 构建

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

产物：`build/pgo`、`build/pgo_gen`、`build/pgo_sql`、`build/pgo_tests`。

### 2.3 跑一个例子

```bash
./build/pgo run \
  --input examples/graph_example.json \
  --output /tmp/result.json \
  --db /tmp/runs.db --threads 1
```

标准输出会打印初末残差、锚点、终止状态与**每条边的误差**；完整结果写入
`--output` 指定的 JSON，审计记录写入 `--db` 的 SQLite。

---

## 3. 输入/输出协议

### 3.1 输入图（冻结格式 `format_version: "1.0"`）

```json
{
  "format_version": "1.0",
  "graph_name": "example-pentagon",
  "nodes": [
    {"id": "n0", "x": 0.0, "y": 0.0, "theta": 0.0}
  ],
  "edges": [
    {"id": "e0", "from": "n0", "to": "n1",
     "dx": 2.0, "dy": 0.0, "dtheta": 1.2566370614359172,
     "info": [200,0,0, 0,200,0, 0,0,300]}
  ]
}
```

- 节点位姿 `(x, y, theta)` 既是标识也是**初始猜测**；角度单位弧度。
- 边的 `(dx, dy, dtheta)` 是 `from` 坐标系下 `to` 相对 `from` 的位姿。
- `info` 是按行展开的 **3×3 信息矩阵**（协方差之逆），必须**对称正定**。
- 角度差在残差中统一用 `atan2(sin,cos)` 包裹到 `[-pi, pi]`，正确跨越
  ±π 接缝（例如真值相对角 +0.283 而裸差值是 -6.0 的情形）。

加载时执行严格校验，任一不过即拒绝（退出码 2）：格式版本不符、节点/边字段
缺失、id 重复、端点不存在、自环、非有限数、信息矩阵不对称或非正定
（Cholesky 失败或主元 ≤ 1e-12·最大主元）。

### 3.2 输出结果（schema `pgo-result/1.0`）

包含：输入指纹（规范化 JSON 的 SHA-256）、锚点、求解器终止状态、
`initial/final` 的 `chi2 / cost / rms`、下降比例、优化后全部节点、以及
`edge_errors_before` 与 `edge_errors_after` 两条逐边数组
（裸残差三元组、`chi2`、稳健权重）。

输出采用**原子发布**：先写同目录临时文件 → `fsync` → `rename`，任何中途
失败都不会留下半截结果。

---

## 4. 数学模型

对边 `e: i -> j`，测量 `m = (mx,my,mt)`，信息矩阵 `I = L Lᵀ`：

```
r_e = L · [ R(θ_i)ᵀ (p_j - p_i) - (mx,my) ;
            wrap(θ_j - θ_i - mt) ]
```

最小化 `Σ_e ρ(r_eᵀ r_e)`，`ρ` 为稳健损失（默认 Huber，可选 Cauchy / none）。

- **规范自由度**：SE(2) 整体可做刚体变换，必须固定锚点，否则法方程奇异。
  - 自动模式：每个连通分量固定其最小 id 节点（确定性）。
  - 手动模式：`--anchor` 可重复指定；**每个连通分量都必须有锚点，否则
    显式报错失败**（不会静默地只优化一个分量）。
- **非正定拒绝**：加载期对每个信息矩阵做对称化 + Cholesky，并显式检查
  主元下界，拒绝负特征值/奇异/严重病态矩阵。
- **稳健损失**：错误闭环会得到很小的影响权重，输出逐边给出
  `robust_weight`（满足 Ceres 约定 `ρ'(s)=2w`）。

---

## 5. 冻结输入图版本

内容哈希基于**规范化 JSON**（解析后紧凑、键排序、双精度 `%.17g`），与输入
的空白和键顺序无关；SHA-256 由自带实现真实计算（已用 FIPS 标准向量交叉
验证）。

```bash
# 冻结一个命名版本
./build/pgo freeze --frozen-db /tmp/frozen.db --name loop-v1 \
  --input examples/graph_example.json

# 仅当输入哈希与冻结版本一致才允许优化
./build/pgo run --input examples/graph_example.json \
  --output /tmp/r.json --db /tmp/runs.db \
  --frozen-db /tmp/frozen.db --frozen loop-v1
```

- 同名同哈希重复冻结：幂等成功。
- 同名不同哈希：拒绝（冻结版本不可被偷改）。
- 运行时哈希不匹配：退出码 **3**，打印 `FROZEN MISMATCH`，不做任何优化。

---

## 6. 取消语义（绝不发布半优化结果）

`SIGINT`（Ctrl-C）/`SIGTERM` 设置原子标志，Ceres 迭代回调每次迭代轮询并
返回 `SOLVER_ABORT`。取消后：

- 退出码 **130**，标准输出打印 `RUN_CANCELLED`；
- **不写输出文件**，若存在同名旧文件会被删除，避免误用；
- 优化后位姿与逐边 after 误差**绝不入库**（`run_nodes` 0 行）；
- 仍把该次运行以 `status=cancelled` 记入 SQLite，保留输入指纹、初始残差与
  before 逐边误差、原因（区分 `CANCELLED_BEFORE_SOLVE` /
  `CANCELLED_DURING_SOLVE`），保证可审计。

`--cancel-after-ms N` 是测试钩子，便于确定性地触发取消。

---

## 7. 自动化测试与验收

```bash
# 全部：单元 + 端到端
ctest --test-dir build --output-on-failure

# 仅单元测试
./build/pgo_tests

# 仅端到端验收
bash tests/acceptance.sh build/pgo build/pgo_gen "$PWD"
```

### 一键验收命令（从零）

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  && cmake --build build -j"$(nproc)" \
  && ctest --test-dir build --output-on-failure
```

验收脚本（33 项断言）覆盖题目要求的全部场景：

| 场景 | 夹具 | 验收点 |
| --- | --- | --- |
| 带漂移闭环 | `drift-loop` | 初始位姿含累积漂移，闭环后 `final/initial chi2 < 1%`，逐边误差≈0 |
| 错误闭环 | `bad-loop` | Huber 将假闭环权重压到 < 0.3，正确闭环权重≈1；`none` 时权重恒 1 |
| 断图 | `disconnected` | 自动每分量锚定（2 锚点）；只锚一个分量显式失败且不产出文件 |
| 病态权重 | `illweight` | 含负特征值信息矩阵被拒，退出码 2，不产出文件 |
| 角度跨 ±π | `angle-seam` | 真值处初始 chi2≈0（裸差 -6 被正确包裹为 +0.283） |
| 取消 | `large-grid` | 定时器与真实 SIGINT 均退出 130、无输出、SQLite 标 cancelled |
| 冻结 | 篡改 dx | 哈希不符退出码 3；同名异哈希拒绝；幂等重冻 |

---

## 8. CLI 参考

```
pgo run    --input G.json --output R.json [--db runs.db]
           [--frozen-db F.db --frozen NAME]
           [--robust none|huber|cauchy] [--robust-scale 1.0]
           [--max-iterations 100] [--linear-solver sparse|dense]
           [--threads 4] [--anchor NODE_ID ...] [--cancel-after-ms N]
pgo freeze --frozen-db F.db --name NAME --input G.json
pgo inspect --db runs.db --run RUN_ID

pgo_gen {drift-loop|bad-loop|disconnected|illweight|large-grid|angle-seam}
        [--size N] [--grid N] [--output out.json]
pgo_sql {tables|runs|edges|nodes|frozen|sql} DB ...
```

退出码：`0` 成功（含达到最大迭代数的完整结果）；`1` 求解/系统错误；
`2` 输入非法；`3` 冻结版本不匹配；`130` 被取消。

---

## 9. SQLite 审计结构

- `frozen_graphs(name, sha256, raw_json, created_at)`
- `runs(...)`：状态、输入指纹、配置、连通分量数、初末残差、取消原因等
- `run_anchors(run_id, node_id)`
- `edge_errors(run_id, edge_id, from, to, phase[before|after], e_x,e_y,e_theta, chi2, robust_weight)`
- `run_nodes(run_id, node_id, ord, x, y, theta)` —— 仅成功运行写入

示例：

```bash
./build/pgo_sql runs /tmp/runs.db
./build/pgo_sql edges /tmp/runs.db <RUN_ID> after
./build/pgo_sql nodes /tmp/runs.db <RUN_ID>
```
