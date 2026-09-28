# Interval Root Certifier（区间根认证服务）

对**连续可微的受限表达式**，在闭区间上结合**区间 Newton 迭代**与**二分细分**，
给出经定理认证的唯一根区间。所有区间运算使用 mpmath 的向外舍入区间算术，
认证结论不依赖浮点运气。

## 支持范围

**表达式语言**（其余构造在解析期即拒绝）：

- 一个自变量（默认 `x`），常量 `pi`、`e`，数值字面量
- 二元 `+ - * * **`（`^` 作为 `**` 的别名）、一元 `+ -`
- 函数 `sin cos exp log sqrt`
- 幂：整数指数（任意底数，负指数要求底数不含 0）；非整数指数要求底数严格为正

**认证定理**（每个认证根都附带可独立复核的证据）：

1. `0 ∉ f(X)` ⇒ X 内无根（排除）。
2. `0 ∈ f'(X)` ⇒ 唯一性定理条件不满足，**保守二分**。
3. `0 ∉ f'(X)` 且区间 Newton 像 `N(X) = m − f(m)/f'(X) ⊆ X`
   ⇒ X 内存在根（Newton 存在性）且严格单调 ⇒ 根唯一。
   随后在 `N(X) ∩ X` 上继续 Newton 收缩，把包含区间收紧到 `tol`。

**结果分类**（三者分别返回，绝不混淆）：

- `certified_roots`：满足上述定理的区间，附完整证据（初始区间、导数区间、
  Newton 像、包含裕度、收紧步数）。
- `undecided`：在宽度/深度/资源预算内无法判定的区间，附原因
  （`min_width_derivative_straddles_zero`、`min_width_newton_image_not_contained`、
  `max_depth_reached`、`resource_exhausted`）。重根（如 `(x-1)^2`）因导数在根处
  为零，**永远落在此类**——这是定理边界，不是缺陷。
- `approximations`：用 `mpmath.findroot` 得到的**数值近似根**，与认证区间
  严格分开放置，且每条都带 `"certified": false`。

## 关键取舍

- **区间内包含 0 ≠ 有根**：只有满足定理条件才认证；否则细分或判未决。
- **非严格包含 + ε 膨胀（Rump 技巧）**：根恰好落在二分边界上时，
  严格包含永远失败。此时在略微膨胀的区间 X* 上**重新验证**全部定理条件
  （`0 ∉ f'(X*)` 且 `N(X*) ⊆ X*`），保持严格性；代价是认证区间可能略微
  超出原始搜索区间（在返回的区间端点中如实可见）。
- **相邻认证合并**：边界根可能被左右两个盒子分别认证，交集非空的认证
  区间会被合并，所有证据 witness 全部保留。
- **除以含零区间 = 定义域错误**：mpmath 对此静默返回 `[-inf, +inf]`，
  会破坏 Newton 步的严格性，因此包装层显式拒绝并保留出错位置。
- **定义域错误保留位置**：`log`/`sqrt`/除法/非整数幂越界时返回表达式树内
  的路径（如 `root/right`）、出错区间与原因。
- **近似与认证分离**：认证只来自区间 Newton 定理；`findroot` 仅用于
  给出好看的数值，永不参与认证。
- **资源预算**：`max_steps` / `max_intervals` / `max_depth` 耗尽时返回
  `resource_exhausted` 与已完成的部分结果，剩余盒子进入 `undecided`。

## 目录结构

```
src/interval_cert/
  intervals.py     # mpmath 区间算术的显式包装（向外舍入、含零除法策略）
  expressions.py   # 受限表达式：解析/校验/求值（区间+点）/符号求导
  kernel.py        # 认证内核：区间 Newton + 二分 + ε 膨胀 + 合并
  evidence.py      # 证据与轨迹记录（run_id、步骤、动作、理由）
  errors.py        # 错误分类：input/state_conflict/domain/resource/compute
  service.py       # FastAPI 接口层：契约校验、HTTP 映射、JSONL 运行日志
tests/             # 43 个测试：区间层/表达式层/内核验收/服务契约
logs/runs.jsonl    # 每次运行一条 JSONL（请求、状态、统计、完整 trace）
```

## 本地启动

```bash
pip install -r requirements.txt          # 依赖已锁定版本
PYTHONPATH=src python3 -m uvicorn interval_cert.service:app --port 8000
# 运行日志目录可用 INTERVAL_CERT_LOG_DIR 覆盖（默认 ./logs）
```

## 示例请求

```bash
# 认证 sqrt(2)：200，certified_roots 恰含一个区间，附 Newton 证据
curl -s -X POST http://127.0.0.1:8000/certify -H 'Content-Type: application/json' -d '{
  "expression": "x^2 - 2",
  "variable": "x",
  "interval": {"lo": 0.0, "hi": 2.0},
  "options": {"tol": 1e-12}
}'

# 重根：200，certified_roots 为空，undecided 非空（定理条件不满足）
curl -s -X POST http://127.0.0.1:8000/certify -H 'Content-Type: application/json' -d '{
  "expression": "(x-1)^2", "interval": {"lo": 0.0, "hi": 2.0}
}'

# 定义域错误：422，error.details 中保留出错位置与区间
curl -s -X POST http://127.0.0.1:8000/certify -H 'Content-Type: application/json' -d '{
  "expression": "log(x)", "interval": {"lo": -1.0, "hi": 1.0}
}'
```

## 错误类别与 HTTP 映射

| 类别 | HTTP | 含义 |
|---|---|---|
| `input_error` | 400 | 表达式非法、区间 lo≥hi、参数非法 |
| `state_conflict` | 409 | 参数各自合法但互相矛盾（如 tol 低于 dps 可分辨精度） |
| `domain_error` | 422 | 求值越出定义域，保留表达式内位置 |
| `resource_exhausted` | 200（`status` 字段区分） | 预算耗尽，返回部分结果 |
| `compute_failure` | 500 | 未预期的内部错误 |

## 测试与可重放性

```bash
python3 -m pytest tests/ -q                 # 44 passed（含覆盖率约 90%）
python3 -m pytest tests/ --cov=interval_cert --cov-report=term
```

- 参考答案独立生成：解析常量（√2、π、e、ln 2）或由 mpmath 80 位精度
  `findroot` 提供，不由被测内核产生。
- 每条认证都带 `run_id` 与完整 `trace`（步骤、动作、区间、理由），
  服务把每次运行追加到 `logs/runs.jsonl`（即使响应中裁剪了 trace，
  日志仍保留完整 trace），可据此离线重放问题。
- 最近一次运行：**44 passed, 0 failed, 0 skipped**（pytest 9.1.1，
  Python 3.12.3）；覆盖率：kernel 91%，expressions 83%，全项目 90%。

## 已知限制

- 重根/偶次相切根无法认证唯一性（定理边界），返回 `undecided`。
- 区间扩张（dependency problem）可能让无根排除需要更多二分；
  `max_steps` 是最后的兜底。
- ε 膨胀认证的区间可能略超出搜索区间边界（端点如实报告）。
- `tan`、`abs` 等不在语言内：`tan` 在极点处不连续，`abs` 不可微。
