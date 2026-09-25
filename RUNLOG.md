# 运行记录（RUNLOG）

环境：Linux 6.8、Python 3.12.3、NumPy 2.5.3（`pip install --user
--break-system-packages numpy`，系统 Python 受 PEP 668 限制且无 venv 模块）、
pytest 9.1.1（系统已预装）。无网络服务、无前端、无求解器第三方依赖。

## 自动化测试

命令：

```bash
python -m pytest tests/ -v
```

结果（2026-09-23 实际运行）：

```
63 passed in 1.92s
```

覆盖：

| 文件 | 用例数 | 内容 |
| --- | --- | --- |
| test_basic_lp.py | 17 | 经典小整数问题、min/max、等式/<=/>=、上下界、固定变量、目标常数、三种终态 |
| test_degeneracy.py | 12 | 退化（零步长）、重复/蕴含/矛盾冗余、零行、Beale 循环（Bland / Dantzig 回退 / 底层第 6 步复现）、随机退化 |
| test_enumeration.py | 9（含参数化 6 组 + 60 随机题） | 顶点枚举参考解逐一对照最优值与解点 |
| test_json_api.py | 20（参数化 7 组） | 两种约束写法、证书与射线响应、界、空框、非法输入、规模上限、CLI 退出码/stdin |
| test_random_stress.py | 5 | 400 随机题三状态全核验、证书专项、50×60 中规模性能、近病态、已知整数答案 |

未通过项：最终提交版本中**无失败、无跳过**。开发过程中修过的真实问题：
目标常数在变量平移时被重复计入；负右端行翻转时漏掉松弛列符号；
无约束分支射线起点未加回下界平移；Farkas 证书在取反行上的乘子方向。
均有对应用例回归。

## 3000 题模糊测试（非 pytest，手动执行）

命令（约 3 秒）：

```bash
PYTHONPATH=src python - <<'PY'
import sys; sys.path.insert(0,'tests')
import numpy as np
from blp import make_lp, solve_lp
from blp.verify import ray_residuals, solution_residuals, verify_certificate
from test_random_stress import _random_problem
from helpers import canonical_arrays
rng = np.random.default_rng(20260923)
counts = {"optimal":0,"infeasible":0,"unbounded":0,"other":0}
fails = []
for i in range(3000):
    kw = _random_problem(rng)
    lp = make_lp(**kw); r = solve_lp(lp)
    counts[r.status if r.status in counts else "other"] += 1
    A_le,b_le,A_eq,b_eq,ub = canonical_arrays(lp)
    if r.status=="optimal":
        res = solution_residuals(r.x-lp.shift, A_le,b_le,A_eq,b_eq,ub)
        if not res["feasible"]: fails.append((i,"opt",res))
    elif r.status=="unbounded":
        res = ray_residuals(r.x-lp.shift, r.ray, A_le,b_le,A_eq,b_eq,ub)
        if not res["valid"]: fails.append((i,"ray",res))
    elif r.status=="infeasible":
        chk = verify_certificate(r.certificate["rows"], A_le,b_le,A_eq,b_eq,ub)
        if not chk["valid"]: fails.append((i,"cert",chk))
print("counts:", counts); print("failures:", len(fails))
PY
```

实际输出：

```
counts: {'optimal': 1264, 'infeasible': 1327, 'unbounded': 409, 'other': 0}
failures: 0
```

每题的结论都用与求解器内部无关的独立核验确认（最优解逐约束残差、
射线方向条件、Farkas 三条件）；没有出现 `numeric_failure` 或
`iteration_limit`。

## 样例 CLI 运行

```bash
for f in examples/0*.json; do
  PYTHONPATH=src python -m blp.cli "$f" -o "examples/responses/$(basename $f .json).response.json"
done
```

结果：

| 请求 | status | objective / 证据 |
| --- | --- | --- |
| 01_optimal_matrix | optimal | -36，x=(2,6)，残差 0 |
| 02_row_form_mixed | optimal | 1.3（= 与 >=、上界混用） |
| 03_infeasible | infeasible | 证书乘子 (1,-1)，muTb=-1，independent_check.valid=true |
| 04_unbounded | unbounded | 射线 (1,1)，变化率 -1，射线核验 valid=true |
| 05_bounds | optimal | 17（正下界平移） |
| 06_max_redundant | optimal | 36（max + 重复约束） |

错误输入退出码实测：非法 JSON 与维度不符均返回 exit=2 与
`status=invalid_input`。

## 已知未做 / 不声称的事

- 不做前端、不做整数/自由变量、不做灵敏度分析；
- 不承诺大规模稀疏性能（规模上限 200 变量 / 200 约束）；
- 极端条件数（约 9 个数量级以上）可能返回 `numeric_failure`——
  随机测试未触发，属于设计上的诚实失败通道。
