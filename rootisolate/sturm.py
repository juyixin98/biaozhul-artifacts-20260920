"""Sturm 链与符号差计数（变号数），全部精确有理数运算。

对无平方多项式 p（与 p' 互素）构造标准 Sturm 序列
        s0 = p,  s1 = p',  s_{i+1} = -rem(s_{i-1}, s_i)
余数每一步做“正”有理数化（只允许正比例缩放）以压制系数增长——
改变任何一项的符号都会破坏 Sturm 变号数定理，因此这里从不做符号归一。

设 V(t) 为序列在 t 处取值后相邻非零值的变号次数（零值一律跳过）。
当 p 在端点不为零时，开区间 (a, b) 内不同实根个数 = V(a) - V(b)。
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np

from . import polynomial as P


def sturm_chain(p: np.ndarray) -> list[np.ndarray]:
    p = P.trim(p)
    if P.is_zero(p):
        raise ValueError("cannot build Sturm chain for zero polynomial")
    if P.degree(p) == 0:
        return [p]

    s0 = p
    s1 = P.derivative(p)
    chain = [s0, s1]
    while P.degree(chain[-1]) > 0:
        _, r = P.divmod_poly(chain[-2], chain[-1])
        nxt = P.scale(r, Fraction(-1))
        if P.is_zero(nxt):
            break  # 理论上互素链不会在此为零；防御性终止
        # 正比例缩放压制系数位数（缩放因子为正，保持各项符号不变）
        g_int, s = P.primitive_positive(nxt)
        chain.append(P.poly(g_int))
    return chain


def eval_chain(chain, x) -> list:
    """链上各多项式在 x 处的精确取值（需要具体值时使用；代价高于 signs_at）。"""
    return [P.horner(s, x) for s in chain]


def signs_at(chain, x):
    """链上各多项式符号（-1/0/1）。变号数只需要符号，故走精确整数快速路径。"""
    return [P.sign_at(s, x) for s in chain]


def variations(signs) -> int:
    """相邻非零值之间的变号次数；零值按 Sturm 约定跳过。"""
    nz = [sg for sg in signs if sg != 0]
    return sum(1 for a, b in zip(nz, nz[1:]) if a != b)


def v_at(chain, x) -> int:
    """p(x) != 0 时的标准变号数 V(x)。"""
    return variations(signs_at(chain, x))


def v_left(chain, x) -> int:
    """V(x^-)：x 是链首多项式 s0 的根时，从左侧趋近的变号数。

    对无平方 p，x 处仅 s0 为零，s1(x) != 0；x 左侧 s0 与 s1 异号，
    因此把 s0 的符号替换为 -sign(s1(x)) 即得左极限。
    """
    sg = signs_at(chain, x)
    s1 = sg[1] if len(sg) > 1 else 1
    sg[0] = -1 if s1 > 0 else 1
    return variations(sg)


def v_right(chain, x) -> int:
    """V(x^+)：x 右侧 s0 与 s1 同号，把 s0 的符号替换为 sign(s1(x))。"""
    sg = signs_at(chain, x)
    s1 = sg[1] if len(sg) > 1 else 1
    sg[0] = 1 if s1 > 0 else -1
    return variations(sg)


def sign_poly(p: np.ndarray, x) -> int:
    return P.sign_at(p, x)
