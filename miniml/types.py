"""Types, unification and type schemes.

Representation
--------------
* :class:`TVar` is a union-find variable. ``link`` points at another variable
  or directly at a structural type (:class:`TCon`/:class:`TApp`). Path
  compression happens in :meth:`TVar.prune`.
* :class:`TCon` is a nullary type constructor (``int``, ``bool``, ``unit``)
  or the binary ``->`` / unary ``ref`` constructor.
* :class:`TApp`` applies a constructor to arguments: ``a -> b`` is
  ``TApp(arrow, [a, b])``; ``ref int`` is ``TApp(refc, [int])``.

Unification performs the **occurs check**: binding a variable to a type that
already contains that variable is rejected, which rules out infinite
(recursive) types such as ``t = t -> t``.

Free-variable collection and scheme instantiation use the classic
``Mark``/``Generic`` trick (a per-operation mark on each variable) instead of
sets, which is the union-find idiom from Chapter 1 of Pierce's *Types and
Programming Languages* / the classic W implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Optional

# Variables are identified by a unique id; free-variable sets are plain
# Python sets of ids (see collect_vars), which is robust to union-find links.


class Type:
    pass


class TVar(Type):
    __slots__ = ("id", "link", "name")

    _ids = count(0)

    def __init__(self, name: Optional[str] = None):
        self.id: int = next(TVar._ids)
        self.link: Optional[Type] = None
        self.name: Optional[str] = name  # debugging hint only

    def prune(self) -> Type:
        """Follow links with path compression; return the representative."""
        link = self.link
        if link is None:
            return self
        if isinstance(link, TVar):
            root = link.prune()
            self.link = root
            return root
        return link

    def __repr__(self) -> str:
        r = self.prune()
        if r is self:
            return f"t{self.id}"
        return repr(r)


@dataclass
class TCon(Type):
    name: str

    def __repr__(self) -> str:
        return self.name


@dataclass
class TApp(Type):
    con: TCon
    args: list[Type] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"{self.con.name}({', '.join(map(repr, self.args))})"


# ------------------------------------------------------------- constructors

t_int = TCon("int")
t_bool = TCon("bool")
t_unit = TCon("unit")
t_arrow = TCon("->")
t_ref = TCon("ref")


def mk_arrow(dom: Type, cod: Type) -> TApp:
    return TApp(t_arrow, [dom, cod])


def mk_ref(inner: Type) -> TApp:
    return TApp(t_ref, [inner])


def is_arrow(t: Type) -> bool:
    return isinstance(t, TApp) and t.con is t_arrow


def is_ref(t: Type) -> bool:
    return isinstance(t, TApp) and t.con is t_ref


def dom(t: TApp) -> Type:
    return t.args[0]


def cod(t: TApp) -> Type:
    return t.args[1]


# ------------------------------------------------------------- free vars


def free_in(t: Type, v: TVar) -> bool:
    """Occurs check: does the unbound variable *v* occur in *t*?"""
    t = t.prune() if isinstance(t, TVar) else t
    if isinstance(t, TVar):
        return t.id == v.id
    if isinstance(t, TApp):
        return any(free_in(a, v) for a in t.args)
    return False


def collect_vars(t: Type, out: set[int]) -> None:
    """Collect the canonical ids of the free variables of *t* into *out*.

    A variable linked to another variable contributes the *representative's*
    id, never its own — otherwise id sets computed before and after a link
    would disagree (which previously let recursive self-references leak into
    a quantified type scheme).
    """
    if isinstance(t, TVar):
        r = t.prune()
        if isinstance(r, TVar):
            out.add(r.id)
        else:
            collect_vars(r, out)
    elif isinstance(t, TApp):
        for a in t.args:
            collect_vars(a, out)


def free_vars(types: list[Type]) -> list[TVar]:
    """Return the distinct free (unbound representative) variables in *types*."""
    by_id: dict[int, TVar] = {}
    for t in types:
        _collect_vars_map(t, by_id)
    return list(by_id.values())


def _collect_vars_map(t: Type, by_id: dict[int, TVar]) -> None:
    if isinstance(t, TVar):
        r = t.prune()
        if isinstance(r, TVar):
            by_id.setdefault(r.id, r)
        else:
            _collect_vars_map(r, by_id)
    elif isinstance(t, TApp):
        for a in t.args:
            _collect_vars_map(a, by_id)


# ------------------------------------------------------------- unification


class UnifyError(Exception):
    """Raised when two types cannot be unified.

    ``cycle`` marks the occurs-check case (infinite type), as opposed to an
    ordinary head constructor clash.
    """

    def __init__(self, message: str, left: Type, right: Type, cycle: bool = False):
        super().__init__(message)
        self.message = message
        self.left = left
        self.right = right
        self.cycle = cycle


def occurs_check(v: TVar, t: Type) -> None:
    if free_in(t, v):
        raise UnifyError(
            f"infinite type: cannot construct the infinite type "
            f"{type_str(v)} = {type_str(t)}",
            v,
            t,
            cycle=True,
        )


def unify(t1: Type, t2: Type) -> None:
    """Destructively unify *t1* and *t2* in place.

    Raises :class:`UnifyError` on a constructor clash or an occurs failure.
    """
    a = t1.prune() if isinstance(t1, TVar) else t1
    b = t2.prune() if isinstance(t2, TVar) else t2

    if isinstance(a, TVar):
        if isinstance(b, TVar) and a.id == b.id:
            return
        occurs_check(a, b)
        a.link = b
        return
    if isinstance(b, TVar):
        # bind the variable on the right; symmetric case
        occurs_check(b, a)
        b.link = a
        return

    # Both structural.
    if isinstance(a, TCon) and isinstance(b, TCon):
        if a.name != b.name:
            raise UnifyError(
                f"type mismatch: {type_str(a)} vs {type_str(b)}", a, b
            )
        return
    if isinstance(a, TApp) and isinstance(b, TApp):
        if a.con.name != b.con.name or len(a.args) != len(b.args):
            raise UnifyError(
                f"type mismatch: {type_str(a)} vs {type_str(b)}", a, b
            )
        for x, y in zip(a.args, b.args):
            unify(x, y)
        return

    raise UnifyError(f"type mismatch: {type_str(a)} vs {type_str(b)}", a, b)


# ------------------------------------------------------------- schemes


@dataclass
class Scheme:
    """A polymorphic type ``forall qvars. body`` (qvars are TVars)."""

    qvars: list[TVar]
    body: Type

    def __str__(self) -> str:
        return scheme_str(self)


def instantiate(sch: Scheme, fresh: "callable") -> Type:
    """Copy *sch*, replacing each quantified variable with a fresh one."""
    mapping: dict[int, TVar] = {q.id: fresh() for q in sch.qvars}
    return _instantiate(sch.body, mapping)


def _instantiate(t: Type, mapping: dict[int, TVar]) -> Type:
    t = t.prune() if isinstance(t, TVar) else t
    if isinstance(t, TVar):
        return mapping.get(t.id, t)
    if isinstance(t, TApp):
        return TApp(t.con, [_instantiate(a, mapping) for a in t.args])
    return t


def generalize(env_types: list[Type], t: Type) -> Scheme:
    """Generalize *t*: quantify over its free vars not free in the environment.

    Variables are tracked by their canonical representative's id, which is
    robust to union-find links created after a variable was generalized.
    """
    env_ids: set[int] = set()
    for x in env_types:
        collect_vars(x, env_ids)
    t_vars: dict[int, TVar] = {}
    _collect_vars_map(t, t_vars)
    qs = [v for i, v in t_vars.items() if i not in env_ids]
    return Scheme(qs, t)


def generalize_closed(t: Type) -> Scheme:
    """Generalize all free variables of *t* (used for closed top-level values)."""
    return Scheme(free_vars([t]), t)


# ------------------------------------------------------------- printing


def _var_names(t: Type) -> dict[int, str]:
    """Assign names a, b, ..., z, a1, b1, ... to the free vars of *t*."""
    fv = free_vars([t])

    def name_for(i: int) -> str:
        if i < 26:
            return chr(ord("a") + i)
        return f"{chr(ord('a') + i % 26)}{i // 26}"

    return {v.id: name_for(i) for i, v in enumerate(fv)}


def type_str(t: Type, names: Optional[dict[int, str]] = None) -> str:
    t = t.prune() if isinstance(t, TVar) else t
    if names is None:
        names = _var_names(t)
    if isinstance(t, TVar):
        return names.get(t.id, f"t{t.id}")
    if isinstance(t, TCon):
        return t.name
    if isinstance(t, TApp):
        if t.con is t_arrow:
            d = type_str(dom(t), names)
            c = type_str(cod(t), names)
            if is_arrow(dom(t)):
                d = f"({d})"
            return f"{d} -> {c}"
        inner = ", ".join(type_str(a, names) for a in t.args)
        return f"{t.con.name} {inner}" if len(t.args) == 1 else f"{t.con.name} ({inner})"
    return str(t)


def scheme_str(sch: Scheme) -> str:
    body = sch.body
    if not sch.qvars:
        return type_str(body)
    names = _var_names(body)
    qnames = [names.get(q.id, f"t{q.id}") for q in sch.qvars]
    return f"forall {', '.join(qnames)}. {type_str(body, names)}"
