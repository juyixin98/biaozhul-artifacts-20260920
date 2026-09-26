# JSON API 规范（差分约束诊断后端）

后端为单个 CLI 可执行文件 `diffcon_solver`：从文件或标准输入读取一个 JSON
请求，向标准输出（或 `-o` 指定文件）写入一个 JSON 响应。**纯后端、无 HTTP
服务、无前端。**

约束形式：`x - y <= c`，其中 `x`、`y` 为整数变量名，`c` 为整数（拒绝浮点）。

## 1. 请求格式

```json
{
  "variables": ["x", "y", "z"],
  "constraints": [
    {"id": "c1", "x": "x", "y": "y", "c": 2}
  ],
  "options": {
    "normalize": true,
    "minimal_candidate": true,
    "reference": "auto"
  }
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `variables` | string[] | 否 | 显式声明的变量名；约束中出现的变量会自动登记。不可重复。 |
| `constraints` | object[] | **是** | 约束数组，每项 `x - y <= c`。 |
| `constraints[].id` | string | 否 | 约束 ID；缺省时取其数组下标字符串。ID 全局唯一。 |
| `constraints[].x` | string | **是** | 左端变量。 |
| `constraints[].y` | string | **是** | 右端变量。 |
| `constraints[].c` | integer | **是** | 整数界，`|c| <= 1,000,000,000`。 |
| `options.normalize` | bool | 否 | 默认 `true`；按连通分量平移归一化（见 §4）。 |
| `options.minimal_candidate` | bool | 否 | 默认 `true`；不可行时求最小矛盾集合候选（见 §5）。 |
| `options.reference` | string | 否 | `"auto"`（默认，n≤300 时跑 Floyd 对照）/ `"on"` / `"off"`。 |

规模限制：变量 ≤ 2000，约束 ≤ 8000。超限返回错误。

## 2. 可行响应

见 `docs/output/01_feasible_disconnected.response.json`。关键字段：

- `feasible`: `true`
- `assignment`: `{变量名: 整数值}`，已独立校验满足**全部**约束；
- `verification.all_satisfied`: 逐条重检 `x - y <= c` 的结果；
- `normalization.components`: 无向连通分量划分（孤立变量单独成一个分量）；
- `evidence`: 主算法（Bellman-Ford）的迭代轮数、松弛次数、耗时，以及朴素
  Floyd-Warshall 参考算法的交叉核对结果（`verdict_matches_primary`、
  `floyd_assignment_verified`）。

## 3. 不可行响应与负环证据

见 `docs/output/03_negative_cycle.response.json`：

- `feasible`: `false`
- `negative_cycle.constraint_ids`: 构成负环的**具体约束 ID**（按环方向有序）；
- `negative_cycle.variable_cycle`: 对应变量环，首尾相同（`closes: true`）；
- `negative_cycle.sum_bounds`: 环上界之和（严格负数）。沿环求和时变量差全部
  望远镜消去，约束推出 `0 <= sum_bounds`，矛盾，因此该环是不可行的直接证据；
- `negative_cycle.explanation`: 上述矛盾的文字说明。

负环提取方法：Bellman-Ford 第 n 轮仍有松弛时，取最后被松弛的顶点沿
前驱指针回退 n 步得到环上一点，再沿前驱闭合成环。

## 4. 解的平移归一化

可行解对每个连通分量整体加常数仍是可行解（约束只含变量差）。默认把**每个
连通分量**平移到"分量内最小值为 0"：

- 连通变量间的相对差不变；
- 不连通分量各自独立归一化（不能用一个全局常数统一处理）；
- 孤立变量归一化后为 0。

`options.normalize=false` 时返回 Bellman-Ford 的原始距离标号
（`normalization.mode = "raw_bellman_ford_labels"`）。

## 5. 最小矛盾集合候选（minimal unsatisfiable subset）

不可行且 `options.minimal_candidate=true` 时返回 `minimal_candidate`：

- 小规模（n ≤ 150 且 m ≤ 500）：**删除过滤法**（deletion filter）。按输入
  顺序逐条尝试删除约束，若删除后仍然不可行则永久删除。结果集合不可行且
  **不可约**（删去其中任意一条，其余即可行）。
- 超出规模阈值：回退为 Bellman-Ford 负环见证（`cycle_witness_fallback`），
  一个简单负环本身即为矛盾集合，但不保证不可约。

**语义说明**：这里的 minimal 指"集合包含意义下不可约"，不保证是全局最小
（minimum）矛盾集合；删除过滤的结果依赖输入顺序。响应中的
`verification.irreducible` 由后端对候选重新执行 Bellman-Ford 独立验证；
Python 测试套件再用独立实现复验一次。

## 6. 错误响应与退出码

```json
{"status": "error", "error": {"code": "invalid_json", "message": "..."}}
```

错误码：`invalid_json`、`invalid_request`、`invalid_options`、
`invalid_variables`、`duplicate_variable`、`missing_constraints`、
`invalid_constraints`、`invalid_constraint`、`duplicate_constraint_id`、
`constraint_out_of_range`、`too_many_variables`、`too_many_constraints`。

| 退出码 | 含义 |
|---|---|
| 0 | 请求合法（无论系统可行与否） |
| 2 | 请求非法（JSON 语法/校验错误） |
| 1 | 用法或文件 IO 错误 |

## 7. CLI 用法

```bash
make
./build/diffcon_solver examples/01_feasible_disconnected.json     # 文件入参
cat request.json | ./build/diffcon_solver                         # 标准输入
./build/diffcon_solver req.json -o resp.json                      # 输出到文件
./build/diffcon_solver --help
```
