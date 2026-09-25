"""实根隔离与二分细化（精确有理数）。

对无平方因子 p，从 Cauchy 根界 (a0, b0) 出发做 Sturm 二分，得到两两不交的
孤立区间，每个区间恰含一个不同实根；分点上精确命中的根单独作为“点根”。
随后在各区间内继续二分直到宽度 <= epsilon，或返回明确的失败状态。

变号数（p 无平方，见 sturm.py）：
    V(t)    p(t) != 0 时的标准变号数；
    V+(t)   p(t)=0 时从右侧趋近的变号数，非根点即 V(t)；
    V-(t)   p(t)=0 时从左侧趋近的变号数，非根点即 V(t)。

递归统一处理“半开”区间。记区间 I、左右端 (l, r)，lr/rr 标记端点是否为
根，则 I 中按如下方式计根：
    n = V+(l) - V-(r) - lr - rr
（半开 (l, r] 时 lr=0；内部开区间在端点根处把对应标志置 1，避免重复计数。）
输出的每个区间叶恒满足：两端都不是根、且 n == 1，于是 va - vb == 1。
"""
from __future__ import annotations

from fractions import Fraction

from . import polynomial as P
from . import sturm as S

_HALF = Fraction(1, 2)


def isolate_factor(chain, p, a0, b0, depth_limit):
    """在 (a0, b0]（半开）内隔离无平方多项式 p 的全部不同实根。

    前置条件：p(a0) != 0 且 p(b0) != 0（Cauchy 界严格包住所有根）。

    返回 (leaves, unresolved)：
      leaves 每项为
        {"kind":"interval","a","b","va","vb","depth"}（两端非根，va-vb==1）
        或 {"kind":"point","r","depth"}（精确有理点根 r）；
      unresolved 为触及 depth_limit 仍含 >1 个根的区间记录（计数可信，
      但未能隔离），为空表示隔离完整。
    """
    leaves, unresolved = [], []
    va0 = S.v_at(chain, a0)
    vb0 = S.v_at(chain, b0)

    # 显式栈：(l, r, vl, vr, depth)。统一语义为“严格位于 (l,r) 内、
    # 尚未被记录为点根的根”。端点变号数约定：
    #   左端用 V+(l)（从右侧趋近，自动排除 l 处的根），
    #   右端用 V-(r)（从左侧趋近，自动排除 r 处的根）。
    # 于是区间内部根数恒为 vl - vr。
    stack = [(a0, b0, va0, vb0, 0)]
    while stack:
        l, r, vl, vr, depth = stack.pop()
        n = vl - vr
        if n == 0:
            continue
        l_root = P.sign_at(p, l) == 0
        r_root = P.sign_at(p, r) == 0
        if n == 1 and not l_root and not r_root:
            # 两端皆非根且内部恰一根：无条件收为区间叶
            leaves.append({"kind": "interval", "a": l, "b": r, "va": vl,
                           "vb": vr, "depth": depth})
            continue
        if depth >= depth_limit:
            unresolved.append(
                {"kind": "interval", "a": l, "b": r, "va": vl, "vb": vr,
                 "depth": depth, "interior_count": n,
                 "left_is_point_root": l_root, "right_is_point_root": r_root})
            continue
        m = (l + r) * _HALF
        if P.sign_at(p, m) != 0:
            vm = S.v_at(chain, m)
            # m 非根：V(m) 同时作为两半的 V+ / V-
            stack.append((m, r, vm, vr, depth + 1))
            stack.append((l, m, vl, vm, depth + 1))
            continue
        # m 精确为根：记录点根；两侧用 V-(m) / V+(m) 排除该点
        leaves.append({"kind": "point", "r": m, "depth": depth + 1})
        vml, vmr = S.v_left(chain, m), S.v_right(chain, m)
        stack.append((m, r, vmr, vr, depth + 1))   # 右内部
        stack.append((l, m, vl, vml, depth + 1))   # 左内部

    leaves.sort(key=lambda L_: (L_["a"] if L_["kind"] == "interval"
                                else L_["r"]))
    unresolved.sort(key=lambda L_: L_["a"])
    return leaves, unresolved


def refine_leaf(chain, p, leaf, eps, extra_depth_limit):
    """把区间叶细化到宽度 <= eps；点根原样返回。

    前置：该区间已由 Sturm 变号数证明恰含一个无平方根（两端非根）。
    故细化只需要对 p 自身做符号二分：无平方单根两侧 p 异号，
    依据 p(mid) 与 p(a) 的符号即可选边，无需每次重算整条 Sturm 链。

    返回 (record, status)：
      ok            已达到宽度，record 含 a,b,va,vb,depth,width；
      exact         细化中（或原本）精确命中根，record 为 point；
      refine_depth  extra_depth_limit 耗尽仍未达到宽度（区间与计数仍可信）。
    record 中保留隔离阶段的变号数 va/vb（恒差 1）作为计数证书。
    """
    if leaf["kind"] == "point":
        return leaf, "exact"
    a, b = leaf["a"], leaf["b"]
    va, vb, iso_depth = leaf["va"], leaf["vb"], leaf["depth"]
    sign_a = P.sign_at(p, a)
    used = 0
    while b - a > eps:
        if used >= extra_depth_limit:
            return ({"kind": "interval", "a": a, "b": b, "va": va, "vb": vb,
                      "depth": iso_depth + used, "isolation_depth": iso_depth,
                      "refinement_steps": used, "width": b - a}, "refine_depth")
        m = (a + b) * _HALF
        sign_m = P.sign_at(p, m)
        if sign_m == 0:
            return ({"kind": "point", "r": m,
                     "depth": iso_depth + used + 1}, "exact")
        if sign_m == sign_a:
            a = m                # 根在右半 (m,b)；新左端符号相同，基准不变
        else:
            b = m                # 根在左半 (a,m)；左端仍为 a，基准不变
        used += 1
    return ({"kind": "interval", "a": a, "b": b, "va": va, "vb": vb,
             "depth": iso_depth + used, "isolation_depth": iso_depth,
             "refinement_steps": used, "width": b - a}, "ok")
