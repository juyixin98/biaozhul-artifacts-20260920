# pgo 协议说明（冻结版本）

本后端实现两个 1.0 协议：

- 输入：`pgo-input/1.0`（字段 `protocol: "pgo-input/1.0"`, `version: "1.0"`）
- 结果：`pgo-result/1.0`

字段解析严格：未知字段忽略，必需字段缺失或类型错误即拒绝（退出码 2）。

## 1. 输入 JSON

```jsonc
{
  "protocol": "pgo-input/1.0",      // 必填，必须精确匹配
  "version": "1.0",                 // 必填，仅接受 "1.0"
  "graph_version": "text48-drift-loop-v3", // 必填非空；冻结输入图版本
  "name": "human readable",         // 可选
  "options": {                      // 可选，全部有默认值
    "anchor_mode": "single",        // single | per_component（默认 single）
    "loss_type": "huber",           // huber | cauchy | none（默认 huber）
    "loss_param": 1.0,              // 稳健损失尺度，必须 > 0
    "max_iterations": 100,          // >= 1
    "function_tolerance": 1e-10,    // Ceres 收敛阈值，>= 0
    "gradient_tolerance": 1e-10,
    "parameter_tolerance": 1e-10,
    "linear_solver": "sparse_cholesky", // sparse_cholesky(默认) | qr
    "num_threads": 1
  },
  "nodes": [
    { "id": "n0", "init": [x, y, theta], "fixed": true }
    // theta 加载时归一化到 (-pi, pi]；fixed 为锚点提示（每分量至多一个）
  ],
  "edges": [
    {
      "id": "e01",                 // 可省略，省略时自动 edge-<下标>
      "from": "n0", "to": "n1",    // 必须引用存在的节点，禁止自环
      "measurement": [dx, dy, dtheta], // from->to 在 from 局部系的相对位姿
      "information": [ /* 9 个数字，3x3 行优先 */ ],
      "loss_type": "default",       // default|none|huber|cauchy
      "loss_param": 1.0             // 省略则用全局
    }
  ]
}
```

### 测量约定

设节点位姿 `T_i = (x_i, y_i, θ_i)`，旋转
`R(θ) = [cosθ -sinθ; sinθ cosθ]`。边 `i -> j` 的测量
`z = (z_x, z_y, z_θ)` 表示在 `i` 局部系下的相对运动，真值满足
`t_j = t_i + R(θ_i) z_xy`，`θ_j = wrap(θ_i + z_θ)`。残差：

```
e_xy = R(z_θ)^T ( R(θ_i)^T (t_j - t_i) - z_xy )
e_θ  = wrap((θ_j - θ_i) - z_θ)
```

代价（无稳健损失）为 `0.5 e^T I e`，`I` 为信息矩阵。角度 wrap 用
`atan2(sin Δ, cos Δ)`，因此跨 ±π 的角度差不会产生 ~2π 假残差。

### 信息矩阵校验

- 必须为 9 个有限数，按行优先填充；
- 不对称（相对偏差 > 1e-9 × 最大模）→ 拒绝；
- 对称化后做特征分解：最小特征值不大于 `1e-16 × max(1, λmax)` →
  以 `information.not_positive_definite` 拒绝（负定、半正定一并拒绝）；
- 条件数 `λmax/λmin > 1e12` → 接受但记录 `information.ill_conditioned` 警告。

### 锚定与连通分量

- 无向连通（BFS）。孤立分量（无边）只警告。
- `single`：分量数必须为 1；多个 `fixed:true` 节点时报 `anchor.ambiguous`。
- `per_component`：每个分量一个锚点（显式 fixed 优先，否则取最小 id）。
- 选锚失败 / 单锚模式遇到多分量 → 退出码 3，无结果文件。

## 2. 结果 JSON（pgo-result/1.0）

```jsonc
{
  "protocol": "pgo-input 同级", "version": "1.0",
  "body": {
    "protocol": "pgo-result/1.0", "version": "1.0",
    "graph_version": "...", "input_sha256": "<指纹>",
    "run_id": "...", "created_at": "2026-09-23T10:02:57Z",
    "tool": { "name": "pgo-se2", "version": "1.0.0", "ceres_version": "2.2.0" },
    "status": "solved",            // solved | failed | cancelled
    "exit_reason": "converged",
    "summary": {
      "num_nodes": 14, "num_edges": 14, "num_components": 1,
      "anchors": ["n0"]
    },
    "residual": {
      "initial": { "weighted_cost": 43.4, "raw_squared": ..., "robust_cost": ... },
      "final":   { "weighted_cost": 3.9e-25, ... }
    },
    "solve": { "iterations": 6, "max_iterations": 100, "elapsed_ms": 0.43,
               "termination": "CONVERGENCE", "message": "..." },
    "nodes": [
      { "id": "n0", "anchor": true,
        "init": [x,y,θ], "final": [x,y,θ] }   // final θ 归一化到 (-pi,pi]
    ],
    "edges": {
      "initial": [ { "edge_id","from","to",
                     "error":[ex,ey,eθ],       // 局部系 3 维残差（θ 已 wrap）
                     "raw_norm":||e||,
                     "weighted_squared": e^T I e,
                     "robust_cost": 0.5 ρ(e^T I e) } ],
      "final":   [ /* 同结构，每条边与输入同序 */ ]
    }
  },
  "manifest": {
    "body_canonical_form": "sorted-key JSON, 2-space indent, 17-digit doubles",
    "hash_algorithm": "SHA-256",
    "body_sha256": "<sha256(canonical(body))>",
    "signed": true,                         // 签名时才出现下列字段
    "hmac_algorithm": "HMAC-SHA-256",
    "hmac_message": "body_sha256",
    "hmac_sha256": "<hmac(key, body_sha256)>"
  }
}
```

失败/取消时**不产出结果文件**；相应信息只进 SQLite（见下）。

### 规范化 JSON（用于哈希）

对象键按 Unicode 码点排序；2 空格缩进；`,`/`:` 后各一空格；
double 用 17 位有效数字（往返无损）；非有限数直接报错；字符串最小转义。
该表示与输入排版无关，故同一输入图无论缩进/键序如何，`input_sha256` 恒定。

## 3. SQLite 运行日志（`--db`，默认 `pgo_runs.db`）

- `runs`：每次调用一行（solved/failed/cancelled），含输入指纹、图版本、
  锚定方式、节点/边/分量数、锚点 JSON、初末代价、迭代数、耗时、结果路径
  与结果文件 SHA-256、是否签名、工具/Ceres 版本。
- `edge_errors`：每次运行的 `initial` 逐边误差；`solved` 另写 `final`。
- `meta`：schema_version 与两个协议名。
- 开启 WAL 与外键。取消行存在但 `result_path`/`result_sha256` 为空。

## 4. 密码操作

全部调用 OpenSSL 3：摘要 `EVP_Q_digest(...,"SHA256",...)`，
MAC 用 `HMAC()`/`EVP_sha256()`。SHA-256 空串与 `"abc"`、HMAC-SHA256
RFC 4231 case 1 作为单元测试向量。MAC 比较使用常量时间逐字节异或。
