# 运行记录（RUNLOG）

本文件如实记录本项目在交付环境中的实际运行命令与结果。

- 日期：2026-09-23
- 环境：Linux 6.8.0-90-generic（Ubuntu），Python 3.12.3，NumPy 2.5.3
- 依赖：仅 NumPy（标准库 unittest/JSON 用于测试与接口）

## 1. 生成样例请求

```bash
$ python3 scripts/make_examples.py
写出 examples/01_laplacian.json
写出 examples/02_ill_conditioned.json
写出 examples/03_zero_rhs.json
写出 examples/04_identity_no_precond.json
写出 examples/05_invalid_csr.json
写出 examples/06_indefinite.json
```

## 2. 自动化测试

```bash
$ ./run_tests.sh        # 等价：生成样例 + python3 -m unittest discover -s tests -v
...
Ran 82 tests in 2.23s

OK
```

退出码 0，**82/82 通过，0 失败、0 错误、0 跳过**。

测试分布：

| 模块 | 测试数 | 覆盖 |
|---|---|---|
| tests/test_csr.py | 22 | CSR 合法构造、matvec 对稠密参考、对角元、零维；越界（正/负）、乱序/重复列、indptr 起点/终点/单调性、长度不符、浮点下标、NaN/Inf、nnz 上限；对称性（结构缺失、数值不等、缩放容差） |
| tests/test_pcg.py | 26 | 已知解（n=50/500）、对角系统、非零 x0、精确初值、零维；病态（cond~2e9，可达/过紧容差）；零右端 3 例；负曲率（Jacobi/无预条件两路径）、零曲率；max_iter、停滞、发散、预条件失效、非法参数、维度不符；Jacobi 对比、真实残差历史、残差替换 n=300 |
| tests/test_api.py | 26 | 端到端已知解、零右端、无预条件、不定矩阵走失败状态、JSON 可序列化；非法 CSR 10+ 例；语义错误（b 长度/类型、缺字段、非对象、未知字段、容差、max_iter、预条件名、非对称、非正/缺对角、preconditioner=none 仍做 SPD 前置检查） |
| tests/test_cli.py | 8 | stdin、文件输入、非法 CSR 退出码、JSON 语法错误、空输入、NaN 常量拒绝、文件不存在、参数过多 |

## 3. CLI 处理全部样例的实际结果

```
$ for f in examples/*.json; do python3 -m sparse_cg.cli "$f"; done
```

| 样例 | ok | status | 迭代数 | 真实残差 ‖b−Ax‖ | 说明 |
|---|---|---|---|---|---|
| 01_laplacian (n=20) | true | converged | 10 | 4.378e-15 | 阈值 2.575e-11 |
| 02_ill_conditioned (n=60, cond≈2.3e9) | true | converged | 60 | 2.189e-05（相对 4.6e-14） | Jacobi，阈值（rtol=1e-10）4.713e-02 |
| 03_zero_rhs (b=0, x0=1) | true | converged | 8 | 8.182e-16 | 解收敛到零向量 |
| 04_identity (无预条件) | true | converged | 1 | 0.0 | 一步精确解 |
| 05_invalid_csr | **false** | — | — | — | error.code = `index_out_of_bounds`（indices[1]=9 越界，范围 [0,2]） |
| 06_indefinite | true（algorithm_failed=true） | **negative_curvature** | 2 | 2.0 | 瑞利商 pᵀAp/pᵀp = −0.6 < 0 |

每个请求的完整 JSON 输出保存在 `examples/responses/`。

## 4. 独立验收演示（不依赖测试框架）

```bash
$ python3 scripts/acceptance_demo.py
```

要点（残差均为脚本独立重算 `‖b−Ax‖`，并与求解器自报值核对一致）：

- A. 良态拉普拉斯 n=200（已知随机解）：Jacobi 与无预条件均在 200 次迭代内
  达到真实残差 4.62e-10（rtol=1e-10 阈值 3.73e-09），解误差 ‖x−x*‖=1.16e-10。
  注：Jacobi 对一维常系数拉普拉斯无提速（其对角元全相等），这是预条件
  本身的性质，已在 README 说明；其有效场景见 B 与测试
  `test_jacobi_reduces_iterations_on_scaled_system`。
- B. 病态缩放系统 n=60，稠密特征值估计条件数 2.302e9：
  rtol=1e-12 时 60 步收敛，相对残差 1.54e-14，解误差 2.27e-11；
  rtol=atol=0（数学不可达）时第 180 次迭代判 `stagnated`（相对残差
  已降到 4.05e-16，残差进入数值地板），返回当前最好的 x，未谎报收敛。
- C. 零右端 b=0、随机非零 x0：40 步收敛到零向量，真实残差 1.70e-13 ≤ atol。
- D. 不定矩阵 [[1,2],[2,1]]：第 2 次迭代报 `negative_curvature`，
  瑞利商 −0.6。
- E. 非法 CSR：`ok=false`，`index_out_of_bounds`。

## 5. 开发过程中实际出现并已修复的问题（如实记录）

初版测试**并非一次通过**（首轮 82 个测试中 17 失败、7 错误）。失败暴露了
3 个真实算法缺陷和若干测试夹具错误，均已修复并由回归测试覆盖：

1. **曲率判据未归一化（真实缺陷）**：初版用 `|pᵀAp| ≤ 1e-12·max|Aij|`
   判零曲率。搜索方向 p 在近收敛时本身很小，正常的 pᵀAp 被误判为“奇异”，
   导致良态拉普拉斯跑到 161 步误报 `zero_curvature`。修复：改用与方向尺度
   无关的瑞利商 `pᵀAp/pᵀp` 判定。新增/对应测试：
   `test_laplacian_small`、`test_history_is_true_residual`、
   `test_zero_rhs_nonzero_x0_returns_to_zero` 等。
2. **周期“完全重启”破坏共轭性（真实缺陷）**：初版每 30 步用真实残差重建
   搜索方向 `p=z`（整体重启），丢掉了 CG 的共轭性，n=500 系统在默认
   5000 步预算内无法收敛到 1e-8。修复：改为残差替换（只校正 r，延续 p 与
   β 递推）。对应测试：`test_laplacian_larger`、
   `test_residual_replacement_keeps_recursive_residual_honest`。
3. **预条件内积 rᵀz 判据过严（真实缺陷）**：初版要求每步 `rᵀz > 0`，
   近收敛时该正值自然趋零，曾误报 `preconditioner_breakdown`。修复：按
   `‖r‖·‖z‖` 归一化，仅对“显著为负/非有限”判失效。对应测试：
   `test_preconditioner_none`、`test_max_iterations_returns_current_best`。
4. **测试夹具错误（非产品缺陷）**：若干 CSR 夹具 indptr 长度/起止值不自洽、
   空整数数组被 numpy 推成 float 导致零维构造失败、负曲率预期迭代次数
   （实际 2 次）、停滞测试误用“对角阵两步精确解”场景等，均已修正。
5. **CLI 对 NaN 常量的拦截**：Python json 默认接受 `NaN`/`Infinity`，
   `parse_constant` 回调对嵌套位置不可靠，改为“解析后递归拒绝非有限浮点”，
   并对文本尾随内容报 `invalid_json`。对应测试：`test_nan_constant_rejected`。
6. **CSR 构造器检查顺序**：调整为 indptr 起点 → 单调性 → 终点，使错误码
   更贴合首要错误原因；空整数数组宽容推断为 intp。

## 6. 已知边界 / 未覆盖项（如实说明）

- 仅支持对称正定系统；非对称矩阵在 API 层即拒绝（`matrix_not_symmetric`）。
- 纯 Python 逐行 matvec，规模上限 n=10 000、nnz=1e6；不面向超大规模/
  高性能场景。
- 只实现 Jacobi（对角）预条件；未实现不完全 Cholesky（IC0/ILU）等。
- 无前端（按需求）。所有交互通过 JSON 文件/stdin 与 Python API。
- 未提供 wheel/包发布配置；按源码目录直接运行（Python 3.10+，NumPy）。
