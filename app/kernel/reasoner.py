"""Reasoning kernel.

Pipeline (each step records provenance so results are explainable):

1. **Flatten** class expressions into atomic nodes. Every intersection gets
   a synthetic node with elimination edges ``I -> operand`` and an
   introduction rule ``operand1, ..., operandN  =>  I``.
2. **Equivalence closure** with a provenance-preserving union-find.
3. **Forward-chaining closure** over the quotient: unary subsumption edges
   plus conjunctive intersection-introduction rules. Closure from a seed
   set gives:
     * class satisfiability -- seed = {the class};
     * instance membership  -- seed = the instance's asserted types.
4. **Unsatisfiability**: class C is unsatisfiable iff its closure contains
   both operands of some disjointness axiom.
5. **Inconsistency**: the whole ontology is inconsistent only when a named
   individual witnesses a contradiction. An unsatisfiable class with no
   individuals does NOT make the ontology inconsistent -- the two outcomes
   are reported separately.

This is a forward-chaining (EL-style) engine: sound and complete for the
supported fragment (subclass / equivalence / disjoint / intersection).
Everything else is refused earlier, at the language layer.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..language import (
    TOP,
    ClassAssertion,
    ClassRef,
    Disjoint,
    Equivalent,
    Expr,
    Ontology,
    SubClass,
)
from .evidence import (
    ConflictPath,
    DerivationStep,
    EquivalenceReport,
    InstanceReport,
    SubsumptionEdge,
    UnsatisfiableReport,
)
from .unionfind import UnionFind

CLASS_UNSATISFIABLE = "CLASS_UNSATISFIABLE"
ONTOLOGY_INCONSISTENT = "ONTOLOGY_INCONSISTENT"


@dataclass(frozen=True)
class _ConjunctionRule:
    """All ``body`` nodes together imply the intersection ``head``."""

    head: str
    body: tuple[str, ...]
    detail: str


@dataclass
class ReasoningResult:
    consistent: bool
    classes: tuple[str, ...]
    equivalences: tuple[EquivalenceReport, ...]
    unsatisfiable: tuple[UnsatisfiableReport, ...]
    instances: tuple[InstanceReport, ...]
    failure_categories: tuple[str, ...]
    steps: tuple[SubsumptionEdge, ...] = field(default_factory=tuple)
    intersection_rules: tuple[_ConjunctionRule, ...] = field(default_factory=tuple)
    unsatisfiable_nodes: tuple[str, ...] = field(default_factory=tuple)
    engine_version: str = ""
    # name -> quotient root, and root -> facts entailed when seeded with root
    _name_root: dict[str, str] = field(default_factory=dict)
    _closures: dict[str, frozenset[str]] = field(default_factory=dict)
    _unsat_roots: frozenset[str] = field(default_factory=frozenset)

    def is_subclass(self, sub: str, sup: str) -> bool:
        """True when ``sub ⊑ sup`` is entailed.

        Reflexive, closed under the TBox, and vacuously true when ``sub`` is
        unsatisfiable (an empty extension is a subclass of every class).
        """
        if sub == sup:
            return True
        sr, tr = self._name_root.get(sub), self._name_root.get(sup)
        if sr is None or tr is None:
            return False
        if sr in self._unsat_roots:
            return True  # ex falso: unsatisfiable ⊑ anything
        return tr in self._closures.get(sr, frozenset())


class _Flattener:
    """Turns expression AST nodes into atomic graph nodes."""

    def __init__(self) -> None:
        self.edges: list[SubsumptionEdge] = []
        self.intro_bodies: dict[str, tuple[str, ...]] = {}
        self.expr_text: dict[str, str] = {TOP: TOP}
        self.synthetic: set[str] = set()
        self.nodes: set[str] = {TOP}

    def flatten(self, expr: Expr) -> str:
        if isinstance(expr, ClassRef):
            self.expr_text.setdefault(expr.iri, expr.iri)
            self.nodes.add(expr.iri)
            return expr.iri
        operand_nodes = tuple(self.flatten(o) for o in expr.operands)
        # Canonical, order-insensitive structural key shared with the oracle,
        # so equivalent intersections in both implementations have the same
        # identity. Display text is kept separately (Unicode rendering).
        node = "and(" + ",".join(sorted(operand_nodes)) + ")"
        self.expr_text[node] = "(" + " ⊓ ".join(
            self.expr_text[n] for n in sorted(operand_nodes)
        ) + ")"
        self.synthetic.add(node)
        self.nodes.add(node)
        self.intro_bodies[node] = operand_nodes
        detail = f"intersection elimination: {self.expr_text[node]}"
        for op in operand_nodes:
            # I ⊑ operand. The converse is applied only when ALL operands
            # hold, via the conjunction introduction rule.
            self.edges.append(SubsumptionEdge(node, op, "<structural>", detail))
        return node


@dataclass
class _AxiomGraph:
    flat: _Flattener
    atomic: list[str]
    assertions: list[tuple[str, str, str]]  # (instance, class node, source)
    disjoint_pairs: list[tuple[str, str, str]]
    equiv_records: list[tuple[tuple[str, ...], str]]


def _axiom_graph(onto: Ontology) -> _AxiomGraph:
    flat = _Flattener()
    assertions: list[tuple[str, str, str]] = []
    disjoint_pairs: list[tuple[str, str, str]] = []
    equiv_records: list[tuple[tuple[str, ...], str]] = []

    for ax in onto.axioms:
        if isinstance(ax, SubClass):
            a, b = flat.flatten(ax.sub), flat.flatten(ax.super)
            flat.edges.append(SubsumptionEdge(a, b, ax.source, "declared subclass"))
        elif isinstance(ax, Equivalent):
            nodes = tuple(flat.flatten(e) for e in ax.operands)
            equiv_records.append((nodes, ax.source))
            for i, a in enumerate(nodes):
                for j, b in enumerate(nodes):
                    if i != j:
                        flat.edges.append(
                            SubsumptionEdge(a, b, ax.source, "equivalence unfolding")
                        )
        elif isinstance(ax, Disjoint):
            nodes = tuple(flat.flatten(e) for e in ax.operands)
            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    disjoint_pairs.append((nodes[i], nodes[j], ax.source))
        elif isinstance(ax, ClassAssertion):
            node = flat.flatten(ax.cls)
            assertions.append((ax.instance, node, ax.source))

    return _AxiomGraph(
        flat, sorted(flat.nodes), assertions, disjoint_pairs, equiv_records
    )


class _Engine:
    """Quotient graph + forward-chaining closure with derivation parents."""

    def __init__(self, graph: _AxiomGraph) -> None:
        self.graph = graph
        flat = graph.flat
        self.uf = UnionFind(set(graph.atomic))
        for nodes, source in graph.equiv_records:
            for other in nodes[1:]:
                self.uf.union(nodes[0], other, source)
        self.canon = {n: self.uf.find(n) for n in graph.atomic}
        self.components = self.uf.components()
        self.raw_edges = self._quotient_edges(flat)
        self.adj: dict[str, list[SubsumptionEdge]] = defaultdict(list)
        for e in self.raw_edges.values():
            self.adj[e.frm].append(e)
        self.rules = self._conjunction_rules(flat)
        self.pair_roots = self._disjoint_pairs(graph.disjoint_pairs)

    # -- construction helpers ------------------------------------------------
    def _quotient_edges(self, flat: _Flattener) -> dict[tuple[str, str], SubsumptionEdge]:
        edges: dict[tuple[str, str], SubsumptionEdge] = {}
        for e in flat.edges:
            ca, cb = self.canon[e.frm], self.canon[e.to]
            if ca != cb and (ca, cb) not in edges:
                edges[(ca, cb)] = SubsumptionEdge(ca, cb, e.source, e.detail)
        return edges

    def _conjunction_rules(self, flat: _Flattener) -> list[_ConjunctionRule]:
        rules: list[_ConjunctionRule] = []
        for inode, operands in flat.intro_bodies.items():
            head = self.canon[inode]
            body = tuple(sorted({self.canon[o] for o in operands}))
            detail = f"intersection introduction: {flat.expr_text[inode]}"
            if not any(r.head == head and r.body == body for r in rules):
                rules.append(_ConjunctionRule(head=head, body=body, detail=detail))
        return rules

    def _disjoint_pairs(self, pairs) -> dict[tuple[str, str], str]:
        roots: dict[tuple[str, str], str] = {}
        for a, b, source in pairs:
            ca, cb = self.canon[a], self.canon[b]
            if ca == cb:  # equivalence collapsed two disjoint operands
                roots[(ca, ca)] = source
            else:
                key = tuple(sorted((ca, cb)))
                roots.setdefault(key, source)  # type: ignore[arg-type]
        return roots

    # -- queries --------------------------------------------------------------
    def label(self, root: str) -> str:
        members = self.components[root]
        named = sorted(m for m in members if m not in self.graph.flat.synthetic)
        if named:
            return named[0]
        return self.graph.flat.expr_text[next(iter(members))]

    def named_members(self, root: str) -> set[str]:
        synthetic = self.graph.flat.synthetic
        return {
            m for m in self.components[root] if m not in synthetic and m != TOP
        }

    def close(
        self, seeds: list[str]
    ) -> tuple[set[str], dict[str, tuple[str, DerivationStep]]]:
        """Forward-chaining fixpoint with a derivation-parent forest."""
        facts: set[str] = set(seeds)
        parent: dict[str, tuple[str, DerivationStep]] = {}
        queue = deque(seeds)
        while queue:
            cur = queue.popleft()
            for edge in self.adj.get(cur, ()):
                if edge.to not in facts:
                    facts.add(edge.to)
                    parent[edge.to] = (
                        cur,
                        DerivationStep(
                            self.label(cur), self.label(edge.to),
                            "subsumption", edge.source, edge.detail,
                        ),
                    )
                    queue.append(edge.to)
            for rule in self.rules:
                if rule.head in facts:
                    continue
                if all(b in facts for b in rule.body):
                    anchor = rule.body[0]
                    facts.add(rule.head)
                    parent[rule.head] = (
                        anchor,
                        DerivationStep(
                            self.label(anchor), self.label(rule.head),
                            "intersection_intro", "<structural>", rule.detail,
                        ),
                    )
                    queue.append(rule.head)
        return facts, parent

    def chain_to(
        self,
        target: str,
        seeds: list[str],
        parent: dict[str, tuple[str, DerivationStep]],
    ) -> tuple[DerivationStep, ...]:
        if target in seeds:
            return ()
        steps: list[DerivationStep] = []
        cur = target
        seen = {target}
        while cur not in seeds and cur in parent:
            prev, step = parent[cur]
            steps.append(step)
            if prev in seen:
                break
            seen.add(prev)
            cur = prev
        return tuple(reversed(steps))

    def find_clash(self, facts: set[str]) -> tuple[str, str, str] | None:
        for (da, db), source in self.pair_roots.items():
            if da == db:
                if da in facts:
                    return da, db, source
            elif da in facts and db in facts:
                return da, db, source
        return None

    def conflict(
        self, subject: str, seeds: list[str], clash, parent, category: str
    ) -> ConflictPath:
        da, db, source = clash
        return ConflictPath(
            subject=subject,
            left_chain=self.chain_to(da, seeds, parent),
            right_chain=() if da == db else self.chain_to(db, seeds, parent),
            disjoint_classes=(self.label(da), self.label(db)),
            disjoint_source=source,
            category=category,
        )


def _classify_classes(engine: _Engine) -> tuple[
    dict[str, frozenset[str]], list[UnsatisfiableReport], set[str]
]:
    closures: dict[str, frozenset[str]] = {}
    reports: list[UnsatisfiableReport] = []
    unsat_roots: set[str] = set()
    flat = engine.graph.flat
    for root in engine.components:
        facts, parent = engine.close([root])
        closures[root] = frozenset(facts)
        clash = engine.find_clash(facts)
        if clash is None:
            continue
        unsat_roots.add(root)
        members = tuple(sorted(
            flat.expr_text[m] if m in flat.synthetic else m
            for m in engine.components[root]
        ))
        reports.append(UnsatisfiableReport(
            engine.label(root),
            members,
            CLASS_UNSATISFIABLE,
            engine.conflict(
                engine.label(root), [root], clash, parent, CLASS_UNSATISFIABLE
            ),
        ))
    return closures, reports, unsat_roots


def _classify_instances(engine: _Engine) -> tuple[list[InstanceReport], bool]:
    by_instance: dict[str, list[str]] = defaultdict(list)
    for instance, class_node, _src in engine.graph.assertions:
        by_instance[instance].append(engine.canon[class_node])

    reports: list[InstanceReport] = []
    inconsistent = False
    for instance, roots in sorted(by_instance.items()):
        seeds = sorted(set(roots))
        facts, parent = engine.close(seeds)
        asserted = tuple(sorted({engine.label(r) for r in seeds}))
        entailed_set: set[str] = set()
        for f in facts:
            entailed_set |= engine.named_members(f)
        clash = engine.find_clash(facts)
        conflict = None
        if clash is not None:
            inconsistent = True
            conflict = engine.conflict(
                instance, seeds, clash, parent, ONTOLOGY_INCONSISTENT
            )
        reports.append(InstanceReport(
            instance=instance,
            asserted_types=asserted,
            entailed_types=tuple(sorted(entailed_set)),
            status="in_conflict" if conflict else "satisfiable",
            conflict=conflict,
        ))
    return reports, inconsistent


def _equivalence_reports(engine: _Engine) -> list[EquivalenceReport]:
    graph = engine.graph
    flat = graph.flat
    declaration_sources: dict[str, list[str]] = defaultdict(list)
    for nodes, source in graph.equiv_records:
        for n in nodes:
            if source not in declaration_sources[n]:
                declaration_sources[n].append(source)

    reports: list[EquivalenceReport] = []
    global_chain = tuple(engine.uf.merge_log)
    for root, members in sorted(
        engine.components.items(), key=lambda kv: _label_key(kv, flat)
    ):
        if len(members) == 1:
            continue
        named = sorted(m for m in members if m not in flat.synthetic)
        canonical = named[0] if named else next(iter(members))
        sources = tuple(sorted({
            s for n in members for s in declaration_sources.get(n, [])
        }))
        member_labels = tuple(sorted(
            flat.expr_text[m] if m in flat.synthetic else m for m in members
        ))
        chain = tuple(
            hop for hop in global_chain
            if engine.uf.find(hop.a) == root and engine.uf.find(hop.b) == root
        )
        reports.append(EquivalenceReport(
            canonical=canonical,
            members=member_labels,
            merge_chain=chain,
            declaration_sources=sources,
        ))
    return reports


def _label_key(kv, flat):
    _root, members = kv
    named = sorted(m for m in members if m not in flat.synthetic)
    return (0, named[0]) if named else (1, next(iter(members)))


def reason(onto: Ontology, engine_version: str = "") -> ReasoningResult:
    graph = _axiom_graph(onto)
    engine = _Engine(graph)
    flat = graph.flat

    closures, unsat_reports, unsat_roots = _classify_classes(engine)
    instance_reports, inconsistent = _classify_instances(engine)
    equiv_reports = _equivalence_reports(engine)

    categories: list[str] = []
    if unsat_reports:
        categories.append(CLASS_UNSATISFIABLE)
    if inconsistent:
        categories.append(ONTOLOGY_INCONSISTENT)

    named_classes = tuple(sorted(
        n for n in graph.atomic if n not in flat.synthetic and n != TOP
    ))
    name_root = {
        n: engine.canon[n]
        for n in graph.atomic if n not in flat.synthetic and n != TOP
    }
    # Every node whose quotient root is unsatisfiable is itself unsatisfiable.
    unsat_nodes = tuple(sorted(
        n for n in graph.atomic if engine.canon[n] in unsat_roots
    ))
    return ReasoningResult(
        consistent=not inconsistent,
        classes=named_classes,
        equivalences=tuple(equiv_reports),
        unsatisfiable=tuple(unsat_reports),
        instances=tuple(instance_reports),
        failure_categories=tuple(categories),
        steps=tuple(sorted(engine.raw_edges.values(), key=lambda e: (e.frm, e.to))),
        intersection_rules=tuple(engine.rules),
        unsatisfiable_nodes=unsat_nodes,
        engine_version=engine_version,
        _name_root=name_root,
        _closures=closures,
        _unsat_roots=frozenset(unsat_roots),
    )
