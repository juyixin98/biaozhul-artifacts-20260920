"""类型的内部表示与类型方案。

采用经典算法 W 的「替换」实现（而不是 union-find）：

* :class:`TVar` 是未定类型变量；
* 推导过程中维护 ``dict[str, Type]`` 替换表，:func:`apply` 沿替换链
  走到不可再化约的表示；
* 合一时若需要把变量 ``a`` 绑定到类型 ``t``，先做 occurs-check
  （``a`` 出现在 ``t`` 中则拒绝，防止构造出递归类型）。

类型变量内部名为 ``?0``、``?1``……；最终展示时由 :func:`render`
重新命名为 ``'a``、``'b``……，保证输出稳定可读。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ast

# ---- 类型 -----------------------------------------------------------------


class Type:
    """类型基类（按对象身份比较，不做结构化相等）。"""


@dataclass(frozen=True)
class TVar(Type):
    name: str  # 内部名形如 "?0"；标注产生的变量是 "'a"


@dataclass(frozen=True)
class TCon(Type):
    """零元类型构造器：int / bool / unit。"""

    name: str


@dataclass(frozen=True)
class TApp(Type):
    """类型构造：con(args...)。ref(int) 即 int ref 单元。"""

    con: str
    args: tuple[Type, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TFun(Type):
    param: Type
    result: Type


# 常用类型与构造器
INT = TCon("int")
BOOL = TCon("bool")
UNIT = TCon("unit")


def tref(t: Type) -> Type:
    return TApp("ref", (t,))


def tfun(param: Type, result: Type) -> Type:
    return TFun(param, result)


@dataclass(frozen=True)
class Scheme:
    """类型方案 ``forall vars. body``。"""

    vars: tuple[str, ...]
    body: Type

    @staticmethod
    def monomorphic(t: Type) -> "Scheme":
        return Scheme((), t)


# ---- 替换与自由变量 --------------------------------------------------------

Subst = dict[str, Type]


def apply(subst: Subst, t: Type) -> Type:
    """沿替换链化约类型。"""
    seen: set[str] = set()
    while isinstance(t, TVar):
        if t.name in seen:  # 防御性：替换表自身成环（正常逻辑下不会发生）
            return t
        seen.add(t.name)
        nxt = subst.get(t.name)
        if nxt is None:
            return t
        t = nxt
    if isinstance(t, TFun):
        return TFun(apply(subst, t.param), apply(subst, t.result))
    if isinstance(t, TApp):
        return TApp(t.con, tuple(apply(subst, a) for a in t.args))
    return t


def free_vars(t: Type) -> set[str]:
    """类型中出现的（未被替换消解的）自由变量名。"""
    root = apply({}, t)  # 调用点通常传 {}；这里仅展开结构
    out: set[str] = set()

    def walk(x: Type) -> None:
        if isinstance(x, TVar):
            out.add(x.name)
        elif isinstance(x, TFun):
            walk(x.param)
            walk(x.result)
        elif isinstance(x, TApp):
            for a in x.args:
                walk(a)

    walk(root)
    return out


def free_vars_with(subst: Subst, t: Type) -> set[str]:
    """考虑已有替换后的自由变量。"""
    return free_vars(apply(subst, t))


def scheme_free_vars(s: Scheme) -> set[str]:
    return free_vars(s.body) - set(s.vars)


# ---- 最终渲染：内部变量 -> 'a 'b ... ---------------------------------------


def render(t: Type, subst: Subst | None = None) -> str:
    """把类型渲染成 ML 风格字符串。

    内部变量（``?N``，推导产生）先沿替换链化约，再按编号顺序统一
    重命名为 ``'a``、``'b``……；同一棵类型树里相同内部变量得到相同
    名字，不同变量得到不同名字，编号顺序稳定。标注里手写的变量
    （``'a`` 等）保留原名。
    """
    subst = subst or {}
    root = apply(subst, t)

    # 收集化约后出现的所有内部自由变量，按内部编号排序
    internal: list[str] = sorted(
        (n for n in free_vars(root) if n.startswith("?")),
        key=lambda n: int(n[1:]),
    )
    letters = "abcdefghijklmnopqrstuvwxyz"
    names = {n: f"'{letters[i]}" for i, n in enumerate(internal)}

    def go(x: Type) -> str:
        x = apply(subst, x)
        if isinstance(x, TVar):
            return names.get(x.name, x.name)  # 手写标注变量保留原名
        if isinstance(x, TCon):
            return x.name
        if isinstance(x, TApp):
            inner = " ".join(go(a) for a in x.args)
            return f"{inner} {x.con}"
        if isinstance(x, TFun):
            p = go(x.param)
            if isinstance(apply(subst, x.param), TFun):
                p = f"({p})"
            return f"{p} -> {go(x.result)}"
        raise AssertionError(f"未知类型节点 {x!r}")

    return go(root)


def render_scheme(s: Scheme, subst: Subst | None = None) -> str:
    """渲染类型方案。

    约束变量按其在方案体中出现的顺序（内部编号）渲染为 'a、'b…，
    ``forall`` 列表与函数体使用同一套名字；自由的内部变量（被
    值限制保留的待定单态变量）显示为 ``?_`` 形式。
    """
    subst = subst or {}
    body = apply(subst, s.body)
    text = render(body, subst)

    if not s.vars:
        return text

    # 用 render 的同一套命名：收集方案体中属于被约束集合的内部变量
    bound_internal = [
        n for n in sorted(free_vars(body),
                          key=lambda n: int(n[1:]) if n.startswith("?") else -1)
        if n in set(s.vars)
    ]
    letters = "abcdefghijklmnopqrstuvwxyz"
    names = {n: f"'{letters[i]}" for i, n in enumerate(bound_internal)}
    # 手写标注的约束变量保留原名
    for q in s.vars:
        if not q.startswith("?"):
            names[q] = q
    qs = " ".join(names[q] for q in s.vars if q in names)
    return f"forall {qs}. {text}"


# ---- 标注 AST -> 内部类型 --------------------------------------------------

class AnnError(Exception):
    """类型标注本身写错（如未知构造器）。"""


_KNOWN_CONS = {"int", "bool", "unit", "ref"}


def ann_to_type(a: ast.Ann) -> Type:
    if isinstance(a, ast.AnnVar):
        return TVar(a.name)
    if isinstance(a, ast.AnnCon):
        if a.name not in _KNOWN_CONS:
            raise AnnError(f"未知类型构造器 {a.name!r}")
        args = tuple(ann_to_type(x) for x in a.args)
        if a.name == "ref":
            if len(args) != 1:
                raise AnnError("ref 需要恰好一个类型参数，如 int ref")
            return tref(args[0])
        if args:
            raise AnnError(f"类型 {a.name!r} 不接受类型参数")
        return TCon(a.name)
    if isinstance(a, ast.AnnFun):
        return TFun(ann_to_type(a.param), ann_to_type(a.result))
    raise AssertionError(f"未知标注节点 {a!r}")  # pragma: no cover
