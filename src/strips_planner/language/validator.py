"""Semantic validation and grounding of the rule language.

The validator converts :class:`RawProblem` output of the parser into a
:class:`~strips_planner.models.Problem` whose action schemas have been grounded
into concrete :class:`GroundAction` instances.

Fixed semantics exercised here (see README):

* Positive preconditions require every atom present; negative preconditions
  require every atom *absent*.
* Effects are judged against the **same predecessor state**. A literal that
  is textually the same in a schema's add and del lists contradicts itself for
  every grounding and is a validation error (``ADD_DELETE_CONFLICT``). Distinct
  parameters can still *alias* at grounding time (``?from == ?to``), making an
  action delete and add the very same ground atom; that case is not rejected
  because the application order is fixed as *delete first, then add*, i.e.
  declared add always wins and the atom survives. :func:`apply_action`
  implements exactly that rule, so the outcome never depends on set iteration
  order.
* ``static`` predicates (e.g. resource identities / capacities) may appear in
  preconditions but never in effects.
"""

from __future__ import annotations

from itertools import product
from typing import Iterable

from strips_planner.errors import IssueCode, ProblemValidationError, ValidationIssue
from strips_planner.language.parser import RawAtom, RawProblem, RawSchema
from strips_planner.models import (
    ActionSchema,
    Atom,
    GroundAction,
    Parameter,
    PredicateDecl,
    Problem,
)

MAX_GROUND_ACTIONS_DEFAULT = 50_000


def validate(raw: RawProblem, *, max_ground_actions: int = MAX_GROUND_ACTIONS_DEFAULT) -> Problem:
    ctx = _Context(raw)
    ctx.collect_types_and_objects()
    ctx.collect_predicates()
    ctx.check_init_and_goal()
    schemas = ctx.collect_schemas()
    ground_actions = ctx.ground_all(schemas, max_ground_actions)
    if ctx.errors:
        raise ProblemValidationError(ctx.errors)
    return Problem(
        name=raw.name,
        types=frozenset(raw.types),
        objects={t: tuple(objs) for t, objs in raw.objects.items()},
        predicates=ctx.predicate_decls,
        init=frozenset(ctx.init_atoms),
        goal_pos=frozenset(ctx.goal_pos_atoms),
        goal_neg=frozenset(ctx.goal_neg_atoms),
        schemas=schemas,
        ground_actions=ground_actions,
        notes=raw.notes,
        warnings=tuple(ctx.warnings),
    )


class _Context:
    def __init__(self, raw: RawProblem) -> None:
        self.raw = raw
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []
        self.predicate_decls: dict[str, PredicateDecl] = {}
        self.init_atoms: set[Atom] = set()
        self.goal_pos_atoms: set[Atom] = set()
        self.goal_neg_atoms: set[Atom] = set()

    # -- types / objects ---------------------------------------------------

    def collect_types_and_objects(self) -> None:
        seen_types: set[str] = set()
        for t in self.raw.types:
            if t in seen_types:
                self._err(IssueCode.DUPLICATE_TYPE, f"$.types", f"duplicate type {t!r}")
            seen_types.add(t)
        seen_objects: set[str] = set()
        for type_name, objs in self.raw.objects.items():
            if type_name not in seen_types:
                self._err(
                    IssueCode.UNKNOWN_TYPE,
                    f"$.objects.{type_name}",
                    f"objects declared for unknown type {type_name!r}",
                )
            for obj in objs:
                if obj in seen_objects:
                    self._err(
                        IssueCode.DUPLICATE_OBJECT,
                        f"$.objects.{type_name}",
                        f"object {obj!r} declared more than once",
                    )
                seen_objects.add(obj)

    # -- predicates --------------------------------------------------------

    def collect_predicates(self) -> None:
        for pname, decl in self.raw.predicates.items():
            type_tuple = tuple(decl["types"])
            for i, t in enumerate(type_tuple):
                if t not in self.raw.types:
                    self._err(
                        IssueCode.UNKNOWN_TYPE,
                        f"$.predicates.{pname}.types[{i}]",
                        f"predicate {pname!r} uses unknown type {t!r}",
                    )
            if pname in self.predicate_decls:
                self._err(IssueCode.DUPLICATE_PREDICATE, "$.predicates",
                          f"duplicate predicate {pname!r}")
            self.predicate_decls[pname] = PredicateDecl(
                name=pname, types=type_tuple, static=bool(decl["static"])
            )

    # -- init / goal -------------------------------------------------------

    def check_init_and_goal(self) -> None:
        self.init_atoms.update(self._ground_literals(self.raw.init, "$.init"))
        self.goal_pos_atoms.update(self._ground_literals(self.raw.goal_pos, "$.goal.pos"))
        self.goal_neg_atoms.update(self._ground_literals(self.raw.goal_neg, "$.goal.neg"))
        overlap = self.goal_pos_atoms & self.goal_neg_atoms
        for atom in sorted(overlap, key=str):
            self._err(
                IssueCode.INVALID_GOAL,
                "$.goal",
                f"atom {atom} is required both positively and negatively in the goal",
            )

    def _ground_literals(self, atoms: Iterable[RawAtom], location: str) -> list[Atom]:
        out: list[Atom] = []
        for i, ra in enumerate(atoms):
            atom = self._check_ground_atom(ra, f"{location}[{i}]")
            if atom is not None:
                out.append(atom)
        return out

    def _check_ground_atom(self, ra: RawAtom, location: str) -> Atom | None:
        decl = self.predicate_decls.get(ra.predicate)
        if decl is None:
            self._err(IssueCode.UNKNOWN_PREDICATE, location,
                      f"unknown predicate {ra.predicate!r}")
            return None
        if len(ra.args) != len(decl.types):
            self._err(
                IssueCode.ARITY_MISMATCH, location,
                f"predicate {ra.predicate!r} expects {len(decl.types)} arg(s), got {len(ra.args)}",
            )
            return None
        for i, (arg, expected_type) in enumerate(zip(ra.args, decl.types)):
            if arg.startswith("?"):
                self._err(
                    IssueCode.UNKNOWN_REFERENCE, location,
                    f"parameter {arg!r} is not allowed outside action schemas",
                )
                return None
            actual_type = self._object_type(arg)
            if actual_type is None:
                self._err(
                    IssueCode.UNKNOWN_REFERENCE, f"{location}[arg {i}]",
                    f"object {arg!r} is not declared in 'objects'",
                )
            elif actual_type != expected_type:
                self._err(
                    IssueCode.TYPE_MISMATCH, f"{location}[arg {i}]",
                    f"object {arg!r} has type {actual_type!r}, expected {expected_type!r}",
                )
        return Atom(ra.predicate, tuple(ra.args))

    def _object_type(self, obj: str) -> str | None:
        for type_name, objs in self.raw.objects.items():
            if obj in objs:
                return type_name
        return None

    # -- schemas -----------------------------------------------------------

    def collect_schemas(self) -> tuple[ActionSchema, ...]:
        seen: set[str] = set()
        out: list[ActionSchema] = []
        for i, schema in enumerate(self.raw.actions):
            loc = f"$.actions[{i}]"
            if schema.name in seen:
                self._err(IssueCode.DUPLICATE_ACTION, loc,
                          f"duplicate action name {schema.name!r}")
            seen.add(schema.name)
            out.append(self._build_schema(schema, loc))
        return tuple(out)

    def _build_schema(self, schema: RawSchema, loc: str) -> ActionSchema:
        param_map: dict[str, str] = {}
        for j, p in enumerate(schema.parameters):
            if p.name in param_map:
                self._err(IssueCode.DUPLICATE_PARAMETER, f"{loc}.parameters[{j}]",
                          f"duplicate parameter {p.name!r}")
            if p.type_name not in self.raw.types:
                self._err(IssueCode.UNKNOWN_TYPE, f"{loc}.parameters[{j}].type",
                          f"parameter {p.name!r} uses unknown type {p.type_name!r}")
            param_map[p.name] = p.type_name

        pre_pos = self._check_schema_atoms(schema.pre_pos, param_map, f"{loc}.preconditions.pos")
        pre_neg = self._check_schema_atoms(schema.pre_neg, param_map, f"{loc}.preconditions.neg")
        add = self._check_schema_atoms(schema.add, param_map, f"{loc}.effects.add")
        delete = self._check_schema_atoms(schema.delete, param_map, f"{loc}.effects.del")

        used_params = {arg for group in (schema.pre_pos, schema.pre_neg, schema.add, schema.delete)
                       for atom in group for arg in atom.args if arg.startswith("?")}
        for j, p in enumerate(schema.parameters):
            if p.name not in used_params:
                self._warn(IssueCode.UNUSED_PARAMETER, f"{loc}.parameters[{j}]",
                           f"parameter {p.name!r} never appears in preconditions or effects")

        # Static predicates are immutable: effects cannot mention them.
        for atoms, where in ((add, "add"), (delete, "del")):
            for atom in atoms:
                decl = self.predicate_decls.get(atom.predicate)
                if decl is not None and decl.static:
                    self._err(
                        IssueCode.STATIC_PREDICATE_MODIFIED,
                        f"{loc}.effects.{where}",
                        f"static predicate {atom.predicate!r} cannot be added or deleted",
                    )

        # Same-predecessor add/del conflict rule: a ground grounding can never
        # add and delete the same atom.
        add_set = frozenset(add)
        del_set = frozenset(delete)
        for atom in sorted(add_set & del_set, key=str):
            self._err(
                IssueCode.ADD_DELETE_CONFLICT,
                f"{loc}.effects",
                f"action {schema.name!r} both adds and deletes {atom} "
                "(conflicting effects evaluated from the same predecessor state)",
            )

        return ActionSchema(
            name=schema.name,
            parameters=tuple(Parameter(p.name, p.type_name) for p in schema.parameters),
            pre_pos=tuple(pre_pos),
            pre_neg=tuple(pre_neg),
            add=tuple(add),
            delete=tuple(delete),
            cost=schema.cost,
        )

    def _check_schema_atoms(
        self, atoms: Iterable[RawAtom], params: dict[str, str], location: str
    ) -> list[Atom]:
        out: list[Atom] = []
        for i, ra in enumerate(atoms):
            aloc = f"{location}[{i}]"
            decl = self.predicate_decls.get(ra.predicate)
            if decl is None:
                self._err(IssueCode.UNKNOWN_PREDICATE, aloc,
                          f"unknown predicate {ra.predicate!r}")
                continue
            if len(ra.args) != len(decl.types):
                self._err(IssueCode.ARITY_MISMATCH, aloc,
                          f"predicate {ra.predicate!r} expects {len(decl.types)} arg(s), "
                          f"got {len(ra.args)}")
                continue
            ok = True
            for j, (arg, expected_type) in enumerate(zip(ra.args, decl.types)):
                if arg.startswith("?"):
                    actual_type = params.get(arg)
                    if actual_type is None:
                        self._err(IssueCode.UNKNOWN_REFERENCE, f"{aloc}[arg {j}]",
                                  f"undeclared parameter {arg!r}")
                        ok = False
                    elif actual_type != expected_type:
                        self._err(IssueCode.TYPE_MISMATCH, f"{aloc}[arg {j}]",
                                  f"parameter {arg!r}: {actual_type!r} is not {expected_type!r}")
                        ok = False
                else:
                    actual_type = self._object_type(arg)
                    if actual_type is None:
                        self._err(IssueCode.UNKNOWN_REFERENCE, f"{aloc}[arg {j}]",
                                  f"object {arg!r} is not declared")
                        ok = False
                    elif actual_type != expected_type:
                        self._err(IssueCode.TYPE_MISMATCH, f"{aloc}[arg {j}]",
                                  f"object {arg!r}: {actual_type!r} is not {expected_type!r}")
                        ok = False
            if ok:
                out.append(Atom(ra.predicate, tuple(ra.args)))
        return out

    # -- grounding ---------------------------------------------------------

    def ground_all(
        self, schemas: tuple[ActionSchema, ...], limit: int
    ) -> tuple[GroundAction, ...]:
        grounded: list[GroundAction] = []
        truncated = False
        for schema in schemas:
            domains = [self.raw.objects.get(p.type_name, ()) for p in schema.parameters]
            if any(len(d) == 0 for d in domains):
                continue  # type without objects (already reported if type unknown)
            for combo in product(*domains):
                if len(grounded) >= limit:
                    truncated = True
                    break
                grounded.append(self._ground_one(schema, combo))
            if truncated:
                break
        if truncated:
            self._err(
                IssueCode.GROUNDING_TOO_LARGE,
                "$.actions",
                f"ground action count exceeds limit of {limit}; raise the bound or reduce objects",
            )
        return tuple(grounded)

    def _ground_one(self, schema: ActionSchema, combo: tuple[str, ...]) -> GroundAction:
        binding = tuple((p.name, value) for p, value in zip(schema.parameters, combo))
        subst = dict(binding)

        def sub(atom: Atom) -> Atom:
            return Atom(atom.predicate,
                        tuple(subst.get(a, a) for a in atom.args))

        pre_pos = frozenset(sub(a) for a in schema.pre_pos)
        pre_neg = frozenset(sub(a) for a in schema.pre_neg)
        add = frozenset(sub(a) for a in schema.add)
        delete = frozenset(sub(a) for a in schema.delete)
        args = " ".join(combo)
        label = f"({schema.name} {args})" if args else f"({schema.name})"
        return GroundAction(
            label=label,
            schema_name=schema.name,
            binding=binding,
            pre_pos=pre_pos,
            pre_neg=pre_neg,
            add=add,
            delete=delete,
            cost=schema.cost,
        )

    # -- issue helpers -----------------------------------------------------

    def _err(self, code: str, location: str, message: str) -> None:
        self.errors.append(ValidationIssue(code, location, message))

    def _warn(self, code: str, location: str, message: str) -> None:
        self.warnings.append(ValidationIssue(code, location, message, severity="warning"))
