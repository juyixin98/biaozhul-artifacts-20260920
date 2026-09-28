"""HTN rule language: parser, schema objects and condition evaluation.

Text syntax (S-expression DSL)
------------------------------

Domain::

    (:domain NAME
      (:operator (!op ?a ?b)
          (:pre (p ?a) (not (q ?b)))
          (:del (p ?a))
          (:add (q ?b)))
      (:method m-name (task ?a ?b)
          (:pre (p ?a))
          (:tasks (sub1 ?a) (sub2 ?b)))            ; ordered subtasks
      (:method m-other (task ?a ?b)
          (:pre)
          (:tasks
            (:partial ((sub1 ?a) (sub2 ?b))        ; partial order
                      (:before 0 1)))))

A method requires an explicit *method name* symbol before its task head;
multiple methods may share a task head (tried in declaration order).
Positive precondition literals may introduce existential variables (e.g.
``(link ?a ?next)`` binds ``?next``) usable in subtasks; negated literals
and comparisons may only reference variables bound earlier (safe negation).

Problem::

    (:problem NAME
      (:domain DOMAIN-NAME)
      (:init (f a) (g b))
      (:tasks (top-task a b)))

``(:before i j)`` declares that subtask ``i`` must precede subtask ``j``;
any pair not constrained is unordered.  A bare ``(:tasks t1 t2)`` is a
total (sequential) order.

Variables start with ``?``.  Preconditions are conjunctions of positive
literals, ``(not LIT)`` negations and built-in comparisons
``= != < <= > >=`` evaluated over bindings, never against the state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LangError(ValueError):
    """Base class for rule-language errors (parse / schema / eval)."""

    def __init__(self, message: str, line: int | None = None) -> None:
        prefix = f"line {line}: " if line is not None else ""
        super().__init__(prefix + message)
        self.line = line


class ParseError(LangError):
    pass


class SchemaError(LangError):
    pass


class UnboundVarError(LangError):
    """A variable could not be grounded during substitution/evaluation."""


# ---------------------------------------------------------------------------
# Terms / expressions
# ---------------------------------------------------------------------------

Atom = str | int | float
Expr = tuple[Any, ...]
Bindings = dict[str, Atom]

BUILTIN_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}


def is_var(term: Any) -> bool:
    return isinstance(term, str) and term.startswith("?") and len(term) > 1


@dataclass(frozen=True)
class Literal:
    """A condition literal: ``(not (pred args...))`` when negated."""

    pred: str
    args: tuple[Atom, ...]
    negated: bool = False

    def __str__(self) -> str:
        inner = "(" + " ".join((self.pred, *map(str, self.args))) + ")"
        return f"(not {inner})" if self.negated else inner


@dataclass(frozen=True)
class Operator:
    name: str
    params: tuple[str, ...]
    pre: tuple[Literal, ...]
    add: tuple[tuple[Atom, ...], ...]
    delete: tuple[tuple[Atom, ...], ...]


@dataclass(frozen=True)
class Method:
    name: str  # method name (unique within a task), used in evidence
    task: str  # compound-task head this method reduces
    params: tuple[str, ...]
    pre: tuple[Literal, ...]
    subtasks: tuple[Expr, ...]
    ordered: bool
    order: tuple[tuple[int, int], ...]  # (before-index, after-index) pairs


@dataclass(frozen=True)
class Domain:
    name: str
    operators: dict[str, Operator] = field(default_factory=dict)
    methods: dict[str, list[Method]] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskNetwork:
    """Root task network from a problem (or a method expansion)."""

    tasks: tuple[Expr, ...]
    ordered: bool
    order: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Problem:
    name: str
    domain_name: str
    init: frozenset[tuple[Atom, ...]]
    root: TaskNetwork


# ---------------------------------------------------------------------------
# Tokenizer / S-expression reader
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[tuple[str, int]]:
    tokens: list[tuple[str, int]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split(";", 1)[0]
        for word in line.split():
            i = 0
            while i < len(word):
                ch = word[i]
                if ch in "()":
                    tokens.append((ch, lineno))
                    i += 1
                else:
                    j = i
                    while j < len(word) and word[j] not in "()":
                        j += 1
                    tokens.append((word[i:j], lineno))
                    i = j
    return tokens


def _atom(token: str) -> Atom:
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def parse_sexpr(text: str) -> Expr:
    """Parse one S-expression document; returns the first form."""
    tokens = _tokenize(text)
    if not tokens:
        raise ParseError("empty input")
    pos = 0

    def read(indent: int = 0) -> Expr | Atom:
        nonlocal pos
        if pos >= len(tokens):
            raise ParseError("unexpected end of input")
        token, lineno = tokens[pos]
        if token == "(":
            pos += 1
            items: list[Expr | Atom] = []
            while True:
                if pos >= len(tokens):
                    raise ParseError("missing closing ')'", lineno)
                if tokens[pos][0] == ")":
                    pos += 1
                    return tuple(items)
                items.append(read(indent + 1))
        elif token == ")":
            raise ParseError("unexpected ')'", lineno)
        else:
            pos += 1
            return _atom(token)

    form = read()
    if pos != len(tokens):
        raise ParseError("extra tokens after top-level form", tokens[pos][1])
    if not isinstance(form, tuple):
        raise ParseError("top-level form must be a list")
    return form


def _as_form(value: Any, what: str) -> tuple:
    if not isinstance(value, tuple) or len(value) == 0:
        raise SchemaError(f"{what} must be a non-empty list")
    return value


def _head_is(form: tuple, tag: str) -> bool:
    return bool(form) and form[0] == tag


# ---------------------------------------------------------------------------
# Literal / clause parsing
# ---------------------------------------------------------------------------


def parse_literal(form: tuple) -> Literal:
    form = _as_form(form, "precondition literal")
    if form[0] == "not":
        if len(form) != 2 or not isinstance(form[1], tuple):
            raise SchemaError("'not' wraps exactly one literal")
        inner = _as_form(form[1], "negated literal")
        return Literal(str(inner[0]), tuple(inner[1:]), negated=True)
    if not isinstance(form[0], str):
        raise SchemaError("predicate name must be a symbol")
    return Literal(str(form[0]), tuple(form[1:]))


def _parse_clause_list(form: tuple, tag: str) -> tuple[Literal, ...]:
    if not _head_is(form, tag) or len(form) < 1:
        raise SchemaError(f"expected ({tag} ...) section")
    return tuple(parse_literal(part) for part in form[1:])


def _parse_fact_list(form: tuple, tag: str) -> tuple[tuple[Atom, ...], ...]:
    if not _head_is(form, tag):
        raise SchemaError(f"expected ({tag} ...) section")
    facts = []
    for part in form[1:]:
        f = _as_form(part, f"{tag} fact")
        if not isinstance(f[0], str):
            raise SchemaError("fact predicate must be a symbol")
        facts.append(tuple(f))
    return tuple(facts)


def _vars_of(terms: Iterable[Any]) -> set[str]:
    return {t for t in terms if is_var(t)}


def _check_vars_known(lit: Literal, known: set[str], where: str) -> None:
    free = {a for a in lit.args if is_var(a) and a not in known}
    if free:
        raise SchemaError(f"{where} references unbound variable(s) {sorted(free)}: {lit}")


def _check_safe_preconditions(
    literals: tuple[Literal, ...], params: tuple[str, ...], where: str
) -> None:
    """Safety check for a conjunctive precondition block.

    Parameters are bound from the start.  Positive (non-negated,
    non-comparison) literals may *introduce* new variables that later
    literals and subtask arguments can use (existential quantification).
    Negated literals and comparisons must only reference variables that are
    already bound - the standard 'safe negation' rule.
    """
    bound = set(params)
    for lit in literals:
        vars_in = {a for a in lit.args if is_var(a)}
        if lit.negated or lit.pred in BUILTIN_COMPARISONS:
            free = vars_in - bound
            if free:
                raise SchemaError(
                    f"{where}: {'negation' if lit.negated else 'comparison'}"
                    f" {lit} uses unguarded variable(s) {sorted(free)}; variables"
                    f" must be bound by parameters or an earlier positive"
                    f" precondition"
                )
        else:
            bound |= vars_in
    return bound


# ---------------------------------------------------------------------------
# Task-network parsing
# ---------------------------------------------------------------------------


def parse_task_network(form: tuple) -> TaskNetwork:
    """Parse a ``(:tasks ...)`` form (methods and problems share it)."""
    form = _as_form(form, "task network")
    if form[0] != ":tasks":
        raise SchemaError("expected (:tasks ...)")
    body = form[1:]
    if len(body) == 1 and isinstance(body[0], tuple) and body[0] and body[0][0] == ":partial":
        return _parse_partial(body[0])
    if len(body) == 1 and isinstance(body[0], tuple) and body[0] and body[0][0] == ":ordered":
        inner = body[0]
        return TaskNetwork(tuple(inner[1:]), ordered=True, order=())
    return TaskNetwork(tuple(body), ordered=True, order=())


def _parse_partial(form: tuple) -> TaskNetwork:
    # (:partial (task-expr...) (:before i j) ...)
    if len(form) < 2 or not isinstance(form[1], tuple):
        raise SchemaError(":partial needs a list of task expressions")
    tasks = tuple(form[1])
    constraints: list[tuple[int, int]] = []
    for part in form[2:]:
        p = _as_form(part, ":partial constraint")
        if p[0] != ":before" or len(p) != 3:
            raise SchemaError("partial-order constraint must be (:before i j)")
        i, j = p[1], p[2]
        if not isinstance(i, int) or not isinstance(j, int):
            raise SchemaError(":before indices must be integers")
        if not (0 <= i < len(tasks)) or not (0 <= j < len(tasks)):
            raise SchemaError(f":before index out of range: ({i} {j})")
        if i == j:
            raise SchemaError(f":before self-loop forbidden: ({i} {j})")
        constraints.append((i, j))
    _assert_acyclic(len(tasks), constraints)
    return TaskNetwork(tasks, ordered=False, order=tuple(constraints))


def _assert_acyclic(n: int, edges: list[tuple[int, int]]) -> None:
    succ: dict[int, set[int]] = {i: set() for i in range(n)}
    for i, j in edges:
        succ[i].add(j)
    visiting: set[int] = set()
    done: set[int] = set()

    def dfs(node: int) -> None:
        if node in done:
            return
        if node in visiting:
            raise SchemaError(f"ordering cycle through subtask {node}")
        visiting.add(node)
        for nxt in succ[node]:
            dfs(nxt)
        visiting.discard(node)
        done.add(node)

    for i in range(n):
        dfs(i)


# ---------------------------------------------------------------------------
# Domain / problem parsing
# ---------------------------------------------------------------------------


def _parse_operator(form: tuple) -> Operator:
    if len(form) < 2 or not isinstance(form[1], tuple):
        raise SchemaError("operator needs a head (!name params...)")
    head = _as_form(form[1], "operator head")
    name = str(head[0])
    if not name.startswith("!"):
        raise SchemaError(f"operator name must start with '!': {name}")
    params = tuple(head[1:])
    if any(not is_var(p) for p in params):
        raise SchemaError(f"operator {name} parameters must be variables")
    if len(set(params)) != len(params):
        raise SchemaError(f"operator {name} has duplicate parameters")

    sections = {":pre": None, ":del": None, ":add": None}
    for part in form[2:]:
        p = _as_form(part, "operator section")
        tag = p[0]
        if tag not in (":pre", ":del", ":add"):
            raise SchemaError(f"unknown operator section {tag} in {name}")
        if sections[tag] is not None:
            raise SchemaError(f"duplicate {tag} section in {name}")
        sections[tag] = p
    if any(v is None for v in sections.values()):
        raise SchemaError(f"operator {name} needs :pre/:del/:add sections")

    pre = _parse_clause_list(sections[":pre"], ":pre")
    delete = _parse_fact_list(sections[":del"], ":del")
    add = _parse_fact_list(sections[":add"], ":add")

    known = set(params)
    for lit in pre:
        if lit.pred in BUILTIN_COMPARISONS:
            if len(lit.args) != 2 or lit.negated:
                raise SchemaError(f"comparison {lit.pred} takes 2 args and cannot be negated")
        _check_vars_known(lit, known, f"operator {name} precondition")
    for fact in (*add, *delete):
        free = {a for a in fact[1:] if is_var(a) and a not in known}
        if free:
            raise SchemaError(f"operator {name} effect uses unbound var(s) {sorted(free)}")
    return Operator(name, params, pre, add, delete)


def _parse_method(form: tuple) -> Method:
    # (:method NAME (task ?params...) :sections...) ; NAME is required so
    # failure evidence can tell candidate methods apart.
    if len(form) < 3:
        raise SchemaError("method needs (:method NAME (task params...) ...)")
    if not isinstance(form[1], str):
        raise SchemaError(
            f"method must have a name symbol before its task head, got {form[1]!r}"
        )
    name = str(form[1])
    if name.startswith("!") or name.startswith(":"):
        raise SchemaError(f"illegal method name {name}")
    if not isinstance(form[2], tuple):
        raise SchemaError("method task head must be a list (task ?params...)")
    head = _as_form(form[2], "method head")
    if not isinstance(head[0], str):
        raise SchemaError("method task head must be a symbol")
    task_name = str(head[0])
    if task_name.startswith("!"):
        raise SchemaError(f"method task must not name an operator: {task_name}")
    params = tuple(head[1:])
    if any(not is_var(p) for p in params) or len(set(params)) != len(params):
        raise SchemaError(f"method {name} parameters must be unique variables")

    pre_form = tasks_form = None
    for part in form[3:]:
        p = _as_form(part, "method section")
        if p[0] == ":pre":
            if pre_form is not None:
                raise SchemaError(f"method {name}: duplicate :pre")
            pre_form = p
        elif p[0] == ":tasks":
            if tasks_form is not None:
                raise SchemaError(f"method {name}: duplicate :tasks")
            tasks_form = p
        else:
            raise SchemaError(f"method {name}: unknown section {p[0]}")
    if tasks_form is None:
        raise SchemaError(f"method {name}: missing :tasks")
    pre = _parse_clause_list(pre_form, ":pre") if pre_form is not None else ()
    network = parse_task_network(tasks_form)
    for lit in pre:
        if lit.pred in BUILTIN_COMPARISONS:
            if len(lit.args) != 2 or lit.negated:
                raise SchemaError(f"comparison {lit.pred} takes 2 args and cannot be negated")
    # Positive preconditions may introduce existential variables usable in
    # subtasks; safe-negation rules restrict where those variables appear.
    bound = _check_safe_preconditions(pre, params, f"method {name}")
    for task in network.tasks:
        t = _as_form(task, "subtask")
        if not isinstance(t[0], str):
            raise SchemaError(f"method {name}: subtask head must be a symbol")
        free = {a for a in t[1:] if is_var(a) and a not in bound}
        if free:
            raise SchemaError(f"method {name}: subtask {t[0]} uses unbound var(s) {sorted(free)}")
    return Method(
        name=name,
        task=task_name,
        params=params,
        pre=pre,
        subtasks=network.tasks,
        ordered=network.ordered,
        order=network.order,
    )


def parse_domain(text: str) -> Domain:
    form = parse_sexpr(text)
    if form[0] != ":domain" or len(form) < 2 or not isinstance(form[1], str):
        raise ParseError("document must start with (:domain NAME ...)")
    domain = Domain(name=form[1])
    operators: dict[str, Operator] = {}
    methods: dict[str, list[Method]] = {}
    for part in form[2:]:
        p = _as_form(part, "domain entry")
        if p[0] == ":operator":
            op = _parse_operator(p)
            if op.name in operators or op.name in methods:
                raise SchemaError(f"duplicate operator/task name {op.name}")
            operators[op.name] = op
        elif p[0] == ":method":
            m = _parse_method(p)
            if m.name in operators:
                raise SchemaError(f"method {m.name} clashes with operator name")
            existing = {mm.name for mm in methods.get(m.task, ())}
            if m.name in existing:
                raise SchemaError(f"duplicate method name {m.name} for task {m.task}")
            methods.setdefault(m.task, []).append(m)
        else:
            raise SchemaError(f"unknown domain entry: {p[0]}")
    return Domain(name=domain.name, operators=operators, methods=methods)


def parse_problem(text: str) -> Problem:
    form = parse_sexpr(text)
    if form[0] != ":problem" or len(form) < 2 or not isinstance(form[1], str):
        raise ParseError("document must start with (:problem NAME ...)")
    name = form[1]
    domain_name: str | None = None
    init: tuple = ()
    tasks_form = None
    for part in form[2:]:
        p = _as_form(part, "problem entry")
        if p[0] == ":domain":
            if len(p) != 2 or not isinstance(p[1], str):
                raise SchemaError("(:domain NAME) takes one symbol")
            domain_name = p[1]
        elif p[0] == ":init":
            init = _parse_fact_list(p, ":init")
        elif p[0] == ":tasks":
            tasks_form = p
        else:
            raise SchemaError(f"unknown problem entry: {p[0]}")
    if domain_name is None:
        raise SchemaError(f"problem {name}: missing (:domain ...)")
    if tasks_form is None:
        raise SchemaError(f"problem {name}: missing (:tasks ...)")
    root = parse_task_network(tasks_form)
    for task in root.tasks:
        t = _as_form(task, "root task")
        if not isinstance(t[0], str):
            raise SchemaError("root task head must be a symbol")
        bad = [a for a in t[1:] if is_var(a)]
        if bad:
            raise SchemaError(f"root task {t[0]} contains unbound variable(s) {bad}")
    return Problem(name, domain_name, frozenset(init), root)


# ---------------------------------------------------------------------------
# Substitution / unification / evaluation
# ---------------------------------------------------------------------------


def subst_term(term: Atom, env: Bindings) -> Atom:
    if is_var(term):
        if term not in env:
            raise UnboundVarError(f"unbound variable {term}")
        value = env[term]
        # Follow chains; variables never appear in stored values but be strict.
        if is_var(value):
            raise UnboundVarError(f"variable {term} bound to variable {value}")
        return value
    return term


def ground_literal(lit: Literal, env: Bindings) -> Literal:
    return Literal(lit.pred, tuple(subst_term(a, env) for a in lit.args), lit.negated)


def ground_fact(fact: tuple, env: Bindings) -> tuple[Atom, ...]:
    return tuple(subst_term(a, env) for a in fact)  # type: ignore[return-value]


def unify_terms(a: Atom, b: Atom, env: Bindings) -> Bindings | None:
    """Mostly-syntactic unify of two terms under (and extending) ``env``.

    Returns extended bindings or ``None`` on mismatch.  Immutably returns a
    new binding dict on success.
    """
    a2 = subst_var_chain(a, env)
    b2 = subst_var_chain(b, env)
    if is_var(a2) and is_var(b2) and a2 == b2:
        return dict(env)
    if is_var(a2):
        return _bind(a2, b2, env)
    if is_var(b2):
        return _bind(b2, a2, env)
    return dict(env) if a2 == b2 else None


def subst_var_chain(term: Atom, env: Bindings) -> Atom:
    seen: set[str] = set()
    while is_var(term):
        if term in seen:
            raise UnboundVarError(f"binding cycle at {term}")
        seen.add(term)
        if term not in env:
            return term
        term = env[term]
    return term


def _bind(var: str, value: Atom, env: Bindings) -> Bindings | None:
    if is_var(value):
        return None  # binding a variable to an unbound variable is unsupported here
    new_env = dict(env)
    new_env[var] = value
    return new_env


def unify_heads(
    params: tuple[str, ...],
    args: tuple[Atom, ...],
    base_env: Bindings,
) -> Bindings | None:
    """Unify a schema head (variables) against call arguments under base env."""
    if len(params) != len(args):
        return None
    env = dict(base_env)
    for param, arg in zip(params, args):
        resolved = subst_var_chain(arg, env)
        if is_var(resolved):
            return None
        ext = unify_terms(param, resolved, env)
        if ext is None:
            return None
        env = ext
    return env


def _compare(pred: str, left: Atom, right: Atom) -> bool:
    if pred == "=":
        return left == right
    if pred == "!=":
        return left != right
    if isinstance(left, str) or isinstance(right, str):
        raise LangError(f"ordering comparison {pred} on non-numeric terms: {left!r} {right!r}")
    if pred == "<":
        return left < right  # type: ignore[operator]
    if pred == "<=":
        return left <= right  # type: ignore[operator]
    if pred == ">":
        return left > right  # type: ignore[operator]
    if pred == ">=":
        return left >= right  # type: ignore[operator]
    raise LangError(f"unknown comparison {pred}")


def literal_holds(lit: Literal, state: frozenset[tuple[Atom, ...]]) -> bool:
    """Evaluate a *fully ground* literal against the state."""
    if lit.pred in BUILTIN_COMPARISONS:
        if lit.negated:
            raise LangError("negated comparison literals are unsupported")
        return _compare(lit.pred, lit.args[0], lit.args[1])
    fact = (lit.pred, *lit.args)
    present = fact in state
    return not present if lit.negated else present


def first_unmet(
    literals: tuple[Literal, ...], env: Bindings, state: frozenset[tuple[Atom, ...]]
) -> Literal | None:
    """Return the first unmet literal in its (ground) form, for evidence."""
    for lit in literals:
        grounded = ground_literal(lit, env)
        if not literal_holds(grounded, state):
            return grounded
    return None


def _match_fact(
    pattern_args: tuple[Atom, ...], fact: tuple[Atom, ...], env: Bindings
) -> Bindings | None:
    """Match a state fact against a literal argument pattern."""
    if len(pattern_args) + 1 != len(fact):
        return None
    extended: Bindings = dict(env)
    for pattern, value in zip(pattern_args, fact[1:]):
        resolved = subst_var_chain(pattern, extended)
        if is_var(resolved):
            extended[resolved] = value
        elif resolved != value:
            return None
    return extended


def _state_index(
    state: frozenset[tuple[Atom, ...]],
) -> dict[tuple[str, int], list[tuple[Atom, ...]]]:
    index: dict[tuple[str, int], list[tuple[Atom, ...]]] = {}
    for fact in state:
        index.setdefault((str(fact[0]), len(fact) - 1), []).append(fact)
    for facts in index.values():
        facts.sort(key=lambda f: tuple(map(str, f)))
    return index


def satisfy_preconditions(
    literals: tuple[Literal, ...],
    base_env: Bindings,
    state: frozenset[tuple[Atom, ...]],
) -> list[Bindings]:
    """Enumerate every grounding (binding extension) satisfying the conjunction.

    Positive literals are matched against state facts and may introduce new
    (existential) variables; negated literals filter when no matching fact
    exists; comparisons filter on bound values.  Evaluation follows literal
    declaration order, which the safety check guarantees is safe (every
    negated/comparison variable is bound earlier).
    """
    index = _state_index(state)
    environments: list[Bindings] = [dict(base_env)]
    for lit in literals:
        next_environments: list[Bindings] = []
        if lit.pred in BUILTIN_COMPARISONS:
            for env in environments:
                if literal_holds(ground_literal(lit, env), state):
                    next_environments.append(env)
        elif lit.negated:
            positive = Literal(lit.pred, lit.args, negated=False)
            for env in environments:
                if not literal_holds(ground_literal(positive, env), state):
                    next_environments.append(env)
        else:
            candidates = index.get((lit.pred, len(lit.args)), ())
            for env in environments:
                for fact in candidates:
                    extended = _match_fact(lit.args, fact, env)
                    if extended is not None:
                        next_environments.append(extended)
        environments = next_environments
        if not environments:
            break
    return environments


def first_failed_literal(
    literals: tuple[Literal, ...],
    base_env: Bindings,
    state: frozenset[tuple[Atom, ...]],
) -> dict[str, Any] | None:
    """Diagnostic: first literal where candidate groundings drop to zero."""
    index = _state_index(state)
    environments: list[Bindings] = [dict(base_env)]
    for lit in literals:
        before = len(environments)
        if lit.pred in BUILTIN_COMPARISONS:
            environments = [
                env for env in environments
                if literal_holds(ground_literal(lit, env), state)
            ]
        elif lit.negated:
            positive = Literal(lit.pred, lit.args, negated=False)
            environments = [
                env for env in environments
                if not literal_holds(ground_literal(positive, env), state)
            ]
        else:
            extended_envs: list[Bindings] = []
            for fact in index.get((lit.pred, len(lit.args)), ()):
                for env in environments:
                    extended = _match_fact(lit.args, fact, env)
                    if extended is not None:
                        extended_envs.append(extended)
            environments = extended_envs
        if not environments:
            sample = _safe_ground_literal(lit, base_env)
            return {
                "literal": str(sample),
                "reason": "negated_match_present" if lit.negated else
                ("comparison_failed" if lit.pred in BUILTIN_COMPARISONS
                 else "no_matching_fact"),
                "groundings_before": before,
            }
    return None


def _safe_ground_literal(lit: Literal, env: Bindings) -> Literal:
    """Ground a literal for diagnostics, leaving unmatched variables as-is."""
    def resolve(term: Atom) -> Atom:
        if is_var(term):
            return env.get(term, term)
        return term

    return Literal(lit.pred, tuple(resolve(a) for a in lit.args), lit.negated)
