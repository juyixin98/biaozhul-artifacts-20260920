//! Rule engine: builds the dependency graph, propagates copyleft burden to a
//! fixed point (cycles included), checks the policy matrix and solves the
//! OR-choice CSP by backtracking, with an exhaustive enumerator for
//! small-instance cross-checks.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::policy::{Copyleft, Policy, PolicyInput};
use crate::spdx::{self, conjunction_text, parse, to_dnf, Expr, LicenseTerm};

/// How one package links against a dependency.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Linking {
    /// Static linking / combined work (conservative default).
    Static,
    /// Dynamic linking. Strong copyleft still propagates by default;
    /// weak copyleft does not.
    Dynamic,
}

impl Linking {
    fn as_str(self) -> &'static str {
        match self {
            Linking::Static => "static",
            Linking::Dynamic => "dynamic",
        }
    }
}

impl Default for Linking {
    /// Conservative default: edges are static unless stated otherwise.
    fn default() -> Self {
        Linking::Static
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PackageInput {
    pub id: String,
    /// SPDX expression subset, e.g. `(MIT OR GPL-3.0-only) WITH` ... parsed as
    /// a full expression; parentheses and AND/OR/WITH are supported.
    pub license: String,
    /// Force the node to be treated as proprietary regardless of the
    /// per-license classification.
    #[serde(default)]
    pub proprietary: bool,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EdgeInput {
    pub from: String,
    pub to: String,
    #[serde(default)]
    pub linking: Linking,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GraphInput {
    pub root: String,
    pub packages: Vec<PackageInput>,
    #[serde(default)]
    pub edges: Vec<EdgeInput>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EvaluateRequest {
    pub graph: GraphInput,
    #[serde(default)]
    pub policy: PolicyInput,
    /// When set, also enumerate up to this many *satisfying* selections and
    /// report them in the response (for brute-force comparison on small
    /// instances).
    #[serde(default)]
    pub enumerate: Option<u64>,
}

/// One precomputed DNF alternative of a package.
#[derive(Debug, Clone)]
struct AltInfo {
    terms: Vec<LicenseTerm>,
    /// Max copyleft strength among the terms after exception adjustment.
    level: Copyleft,
    /// Whether selecting this alternative makes the node proprietary.
    proprietary: bool,
}

#[derive(Debug, Clone)]
struct Node {
    id: String,
    expr: Expr,
    /// Alternatives that passed the per-term allow checks.
    alts: Vec<AltInfo>,
    /// Alternatives rejected up front, with the reason.
    invalid_alts: Vec<(String, String)>,
    force_proprietary: bool,
}

#[derive(Debug, Clone, Copy)]
struct Edge {
    from: usize,
    to: usize,
    linking: Linking,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EdgeDetail {
    pub from: String,
    pub to: String,
    pub linking: &'static str,
}

/// One policy violation on a concrete selection.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Violation {
    /// `not-allowed` | `incompatible-licenses` | `copyleft-into-proprietary`
    /// | `root-copyleft-ceiling`
    pub kind: &'static str,
    pub message: String,
    /// Node ids from the offending package toward the dependency causing it.
    pub path: Vec<String>,
    pub edges: Vec<EdgeDetail>,
    /// Rendered SPDX terms involved.
    pub terms: Vec<String>,
}

impl Violation {
    /// Identity within a single assignment (distinguishes offending terms).
    fn key(&self) -> String {
        format!(
            "{}|{}|{}",
            self.kind,
            self.path.join(">"),
            self.terms.join(";")
        )
    }

    /// Stable identity of the *conflict source* across assignments.
    ///
    /// The trace attached to a violation can vary when burdens travel around
    /// a cycle (different OR choices light up different edges), so grouping
    /// by the raw path would miss an unavoidable conflict. Instead each
    /// violation kind names the minimum stable identity:
    /// - root ceiling: the root plus the set of possible burden-source nodes
    ///   (carried in `terms` is not used; sources are recorded separately);
    /// - copyleft into proprietary: the proprietary node;
    /// - declared incompatibility: the two linked nodes.
    fn coarse_key(&self) -> String {
        match self.kind {
            "root-copyleft-ceiling" | "not-allowed" => {
                // path[0] is the root for ceiling, the package for not-allowed
                format!("{}|{}", self.kind, self.path.first().cloned().unwrap_or_default())
            }
            "copyleft-into-proprietary" => {
                format!("{}|{}", self.kind, self.path.first().cloned().unwrap_or_default())
            }
            _ => {
                let mut nodes: BTreeSet<&str> = self.path.iter().map(String::as_str).collect();
                for e in &self.edges {
                    nodes.insert(e.from.as_str());
                    nodes.insert(e.to.as_str());
                }
                format!("{}|{}", self.kind, nodes.into_iter().collect::<Vec<_>>().join(","))
            }
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Selection {
    /// The chosen conjunction of concrete terms.
    pub expression: String,
    pub terms: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct EnumerationReport {
    total_alternatives: u128,
    complete: bool,
    satisfying_selections: Vec<BTreeMap<String, Selection>>,
    truncated_at: Option<u64>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EvaluateResponse {
    satisfiable: bool,
    selection: Option<BTreeMap<String, Selection>>,
    /// Violations of the returned selection (empty when satisfiable).
    violations: Vec<Violation>,
    /// When unsatisfiable: violations that occur under EVERY possible
    /// selection — the unavoidable conflict core.
    unavoidable_violations: Vec<Violation>,
    cycles: Vec<Vec<String>>,
    assignments_explored: u64,
    enumeration: Option<EnumerationReport>,
    effective_policy: Value,
    disclaimer: String,
}

struct Engine {
    nodes: Vec<Node>,
    edges: Vec<Edge>,
    root: usize,
    policy: Policy,
}

/// Carry a dependency's effective burden across one link.
///
/// Strong copyleft propagates over both static and dynamic links; weak
/// copyleft propagates only over static links (unless the policy opts into
/// checking dynamic links as well).
fn carry(level: Copyleft, linking: Linking, check_dynamic_links: bool) -> Copyleft {
    match level {
        Copyleft::None => Copyleft::None,
        Copyleft::Strong => Copyleft::Strong,
        Copyleft::Weak => match linking {
            Linking::Static => Copyleft::Weak,
            Linking::Dynamic => {
                if check_dynamic_links {
                    Copyleft::Weak
                } else {
                    Copyleft::None
                }
            }
        },
    }
}

impl Engine {
    fn build(req: &EvaluateRequest) -> Result<Engine, String> {
        let g = &req.graph;

        let mut index = BTreeMap::new();
        let mut nodes = Vec::with_capacity(g.packages.len());
        for (i, p) in g.packages.iter().enumerate() {
            if p.id.trim().is_empty() {
                return Err(format!("package at index {i} has an empty id"));
            }
            if index.insert(p.id.clone(), i).is_some() {
                return Err(format!("duplicate package id {}", p.id));
            }
            let expr = parse(&p.license)
                .map_err(|e| format!("invalid SPDX expression for package {}: {e}", p.id))?;
            nodes.push(Node {
                id: p.id.clone(),
                expr,
                alts: Vec::new(),
                invalid_alts: Vec::new(),
                force_proprietary: p.proprietary,
            });
        }
        let root = *index
            .get(&g.root)
            .ok_or_else(|| format!("root package {:?} not found in packages", g.root))?;

        // Merge parallel edges: the stronger (static) linking wins.
        let mut edge_map: BTreeMap<(usize, usize), Linking> = BTreeMap::new();
        for e in &g.edges {
            let from = *index
                .get(&e.from)
                .ok_or_else(|| format!("edge references unknown package {:?}", e.from))?;
            let to = *index
                .get(&e.to)
                .ok_or_else(|| format!("edge references unknown package {:?}", e.to))?;
            edge_map
                .entry((from, to))
                .and_modify(|l| {
                    if e.linking == Linking::Static {
                        *l = Linking::Static;
                    }
                })
                .or_insert(e.linking);
        }
        let edges = edge_map
            .into_iter()
            .map(|((from, to), linking)| Edge { from, to, linking })
            .collect();

        let policy = Policy::build(&req.policy);

        // Precompute DNF alternatives and apply allow-list checks.
        for node in &mut nodes {
            let alternatives = to_dnf(&node.expr);
            for conj in alternatives {
                let mut reasons = Vec::new();
                let mut level = Copyleft::None;
                let mut all_proprietary = !conj.is_empty();
                for term in &conj {
                    if let Some(msg) = policy.term_allowance_error(term) {
                        reasons.push(msg);
                    }
                    match policy.effective_copyleft(term) {
                        Some(c) => level = level.max(c),
                        None => level = level.max(Copyleft::None),
                    }
                    if !policy.is_proprietary(&term.license) {
                        all_proprietary = false;
                    }
                }
                if reasons.is_empty() {
                    node.alts.push(AltInfo {
                        proprietary: node.force_proprietary || all_proprietary,
                        level,
                        terms: conj,
                    });
                } else {
                    node.invalid_alts
                        .push((conjunction_text(&conj), reasons.join("; ")));
                }
            }
            // Preferred choice first: weaker copyleft, non-proprietary,
            // fewer combined terms, then lexicographic for determinism.
            node.alts.sort_by(|a, b| {
                (a.level, a.proprietary, a.terms.len(), conjunction_text(&a.terms))
                    .cmp(&(b.level, b.proprietary, b.terms.len(), conjunction_text(&b.terms)))
            });
        }

        Ok(Engine {
            nodes,
            edges,
            root,
            policy,
        })
    }

    fn edge_detail(&self, e: &Edge) -> EdgeDetail {
        EdgeDetail {
            from: self.nodes[e.from].id.clone(),
            to: self.nodes[e.to].id.clone(),
            linking: e.linking.as_str(),
        }
    }

    /// Fixed-point effective burden per node, plus a parent pointer recording
    /// which incoming edge last raised each node's burden (for path traces).
    fn effective_levels(
        &self,
        assignment: &[usize],
    ) -> (Vec<Copyleft>, Vec<Option<usize>>) {
        let n = self.nodes.len();
        let mut eff: Vec<Copyleft> = (0..n)
            .map(|i| self.nodes[i].alts[assignment[i]].level)
            .collect();
        let mut reason = vec![None; n];
        let mut changed = true;
        // Monotone lattice of height 2 => terminates quickly even with cycles.
        while changed {
            changed = false;
            for (ei, e) in self.edges.iter().enumerate() {
                let carried = carry(
                    eff[e.to],
                    e.linking,
                    self.policy.check_dynamic_links,
                );
                if carried > eff[e.from] {
                    eff[e.from] = carried;
                    reason[e.from] = Some(ei);
                    changed = true;
                }
            }
        }
        (eff, reason)
    }

    /// Evaluate every rule against a complete assignment.
    fn evaluate_assignment(&self, assignment: &[usize]) -> Vec<Violation> {
        let mut violations = Vec::new();
        let (eff, reason) = self.effective_levels(assignment);

        // (0) Incompatibility between licenses combined within one package's
        // own conjunction (e.g. `GPL-2.0-only AND Apache-2.0`).
        for (i, node) in self.nodes.iter().enumerate() {
            let alt = &node.alts[assignment[i]];
            for x in 0..alt.terms.len() {
                for y in (x + 1)..alt.terms.len() {
                    let ta = &alt.terms[x];
                    let tb = &alt.terms[y];
                    if self.policy.licenses_incompatible(&ta.license, &tb.license) {
                        violations.push(Violation {
                            kind: "incompatible-licenses",
                            message: format!(
                                "{} and {} are declared incompatible by the policy matrix \
                                 but are combined within package {}",
                                ta.render(),
                                tb.render(),
                                node.id
                            ),
                            path: vec![node.id.clone()],
                            edges: vec![],
                            terms: vec![ta.render(), tb.render()],
                        });
                    }
                }
            }
        }

        // (1) Explicit license incompatibility across each edge.
        for e in &self.edges {
            if e.linking == Linking::Dynamic && !self.policy.check_dynamic_links {
                continue;
            }
            let a = &self.nodes[e.from].alts[assignment[e.from]];
            let b = &self.nodes[e.to].alts[assignment[e.to]];
            for ta in &a.terms {
                for tb in &b.terms {
                    if self.policy.licenses_incompatible(&ta.license, &tb.license) {
                        violations.push(Violation {
                            kind: "incompatible-licenses",
                            message: format!(
                                "{} and {} are declared incompatible by the policy matrix ({} link)",
                                ta.render(),
                                tb.render(),
                                e.linking.as_str()
                            ),
                            path: vec![self.nodes[e.from].id.clone(), self.nodes[e.to].id.clone()],
                            edges: vec![self.edge_detail(e)],
                            terms: vec![ta.render(), tb.render()],
                        });
                    }
                }
            }
        }

        // (2) Copyleft burden landing on a proprietary node. The burden that
        // matters is the child's effective burden after it is carried across
        // this particular link — the same `carry` used in propagation, so
        // weak-over-dynamic is governed by checkDynamicLinks and the
        // proprietary weak-rejection switch by proprietaryRejectsWeak.
        for e in &self.edges {
            let from_alt = &self.nodes[e.from].alts[assignment[e.from]];
            if !from_alt.proprietary {
                continue;
            }
            let burden = carry(
                eff[e.to],
                e.linking,
                self.policy.check_dynamic_links,
            );
            let triggered = match burden {
                Copyleft::Strong => true,
                Copyleft::Weak => self.policy.proprietary_rejects_weak,
                Copyleft::None => false,
            };
            if triggered {
                let child_terms: Vec<String> = self.nodes[e.to].alts[assignment[e.to]]
                    .terms
                    .iter()
                    .map(|t| t.render())
                    .collect();
                violations.push(Violation {
                    kind: "copyleft-into-proprietary",
                    message: format!(
                        "{} {} dependency {} propagates {} copyleft burden into a proprietary package",
                        self.nodes[e.from].id,
                        e.linking.as_str(),
                        self.nodes[e.to].id,
                        burden.as_str()
                    ),
                    path: vec![self.nodes[e.from].id.clone(), self.nodes[e.to].id.clone()],
                    edges: vec![self.edge_detail(e)],
                    terms: child_terms,
                });
            }
        }

        // (3) Root ceiling: the combined work inheriting more copyleft than
        // the root's own declared license can carry. A proprietary root may
        // accept weak burden when the policy relaxes weak copyleft into
        // proprietary code; strong burden is always over its ceiling.
        let root_alt = &self.nodes[self.root].alts[assignment[self.root]];
        let ceiling = root_alt.level;
        let ceiling_exceeded = if root_alt.proprietary
            && !self.policy.proprietary_rejects_weak
            && eff[self.root] == Copyleft::Weak
        {
            false
        } else {
            eff[self.root] > ceiling
        };
        if ceiling_exceeded {
            let mut path = vec![self.nodes[self.root].id.clone()];
            let mut edges = Vec::new();
            let mut cur = self.root;
            let mut seen = BTreeSet::from([self.root]);
            while let Some(ei) = reason[cur] {
                let e = self.edges[ei];
                edges.push(self.edge_detail(&e));
                path.push(self.nodes[e.to].id.clone());
                if !seen.insert(e.to) {
                    break; // burden feeds back through a cycle
                }
                cur = e.to;
            }
            let terms: Vec<String> = self.nodes[cur].alts[assignment[cur]]
                .terms
                .iter()
                .map(|t| t.render())
                .collect();
            violations.push(Violation {
                kind: "root-copyleft-ceiling",
                message: format!(
                    "effective {} copyleft burden on root {} exceeds its own license strength {}",
                    eff[self.root].as_str(),
                    self.nodes[self.root].id,
                    ceiling.as_str()
                ),
                path,
                edges,
                terms,
            });
        }

        dedup_violations(violations)
    }

    /// Variable ordering for backtracking: root first, then smallest domain,
    /// then id — deterministic and cheap fail-first.
    fn variable_order(&self) -> Vec<usize> {
        let mut order: Vec<usize> = (0..self.nodes.len()).collect();
        order.sort_by_key(|&i| {
            (
                i != self.root,
                self.nodes[i].alts.len(),
                self.nodes[i].id.clone(),
            )
        });
        order
    }

    /// Search for any violation-free assignment. Returns the assignment plus
    /// the number of complete assignments explored.
    fn find_satisfying(&self) -> (Option<Vec<usize>>, u64) {
        let order = self.variable_order();
        let mut assignment = vec![0usize; self.nodes.len()];
        let mut explored = 0u64;
        let found = self.dfs(&order, 0, &mut assignment, &mut explored);
        (found.then(|| assignment.clone()), explored)
    }

    fn dfs(
        &self,
        order: &[usize],
        depth: usize,
        assignment: &mut [usize],
        explored: &mut u64,
    ) -> bool {
        if depth == order.len() {
            *explored += 1;
            return self.evaluate_assignment(assignment).is_empty();
        }
        let node = order[depth];
        for alt in 0..self.nodes[node].alts.len() {
            assignment[node] = alt;
            if self.dfs(order, depth + 1, assignment, explored) {
                return true;
            }
        }
        false
    }

    /// Enumerate every assignment (full DFS). Used for the unavoidable
    /// violation core and brute-force cross-checks.
    fn enumerate_all(
        &self,
        limit: Option<u64>,
        only_satisfying: bool,
    ) -> (Vec<Vec<usize>>, bool, u64) {
        let order = self.variable_order();
        let mut assignment = vec![0usize; self.nodes.len()];
        let mut explored = 0u64;
        let mut collected = Vec::new();
        let mut truncated = false;
        self.enumerate_dfs(
            &order,
            0,
            &mut assignment,
            &mut explored,
            &mut collected,
            limit,
            only_satisfying,
            &mut truncated,
        );
        (collected, truncated, explored)
    }

    #[allow(clippy::too_many_arguments)]
    fn enumerate_dfs(
        &self,
        order: &[usize],
        depth: usize,
        assignment: &mut [usize],
        explored: &mut u64,
        collected: &mut Vec<Vec<usize>>,
        limit: Option<u64>,
        only_satisfying: bool,
        truncated: &mut bool,
    ) {
        if let Some(max) = limit {
            if collected.len() as u64 >= max {
                *truncated = true;
                return;
            }
        }
        if depth == order.len() {
            *explored += 1;
            if !only_satisfying || self.evaluate_assignment(assignment).is_empty() {
                collected.push(assignment.to_vec());
            }
            return;
        }
        let node = order[depth];
        for alt in 0..self.nodes[node].alts.len() {
            assignment[node] = alt;
            self.enumerate_dfs(
                order,
                depth + 1,
                assignment,
                explored,
                collected,
                limit,
                only_satisfying,
                truncated,
            );
            if *truncated {
                return;
            }
        }
    }

    /// Violations present at the same conflict location under EVERY assignment.
    /// Conflict locations ignore concrete terms (so a violation forced by
    /// "any of several OR choices" still surfaces); the returned terms field
    /// unions the terms observed across assignments.
    fn unavoidable_violations(&self) -> (Vec<Violation>, u64) {
        let (all, _, explored) = self.enumerate_all(None, false);
        let mut common: Option<BTreeSet<String>> = None;
        // coarse key -> representative violation with accumulated term list.
        let mut representatives: BTreeMap<String, Violation> = BTreeMap::new();
        for assignment in &all {
            let vs = self.evaluate_assignment(assignment);
            let keys: BTreeSet<String> = vs.iter().map(|v| v.coarse_key()).collect();
            for v in vs {
                let ck = v.coarse_key();
                representatives
                    .entry(ck.clone())
                    .and_modify(|rep| {
                        for t in &v.terms {
                            if !rep.terms.contains(t) {
                                rep.terms.push(t.clone());
                            }
                        }
                    })
                    .or_insert(v);
            }
            common = Some(match common {
                None => keys,
                Some(c) => c.intersection(&keys).cloned().collect(),
            });
        }
        let keys = common.unwrap_or_default();
        let mut out: Vec<Violation> = keys
            .into_iter()
            .filter_map(|k| representatives.remove(&k))
            .collect();
        for v in &mut out {
            v.terms.sort();
            v.terms.dedup();
        }
        out.sort_by_key(|v| v.coarse_key());
        (out, explored)
    }

    /// Tarjan SCC — cycles are SCCs of size >1 or self-loops.
    fn cycles(&self) -> Vec<Vec<String>> {
        let n = self.nodes.len();
        let mut adj = vec![Vec::new(); n];
        for e in &self.edges {
            adj[e.from].push(e.to);
        }
        struct State {
            index: i64,
            stack: Vec<usize>,
            on_stack: Vec<bool>,
            idx: Vec<i64>,
            low: Vec<i64>,
        }
        let mut st = State {
            index: 0,
            stack: Vec::new(),
            on_stack: vec![false; n],
            idx: vec![-1; n],
            low: vec![-1; n],
        };
        let mut sccs: Vec<Vec<usize>> = Vec::new();
        fn strongconnect(
            v: usize,
            adj: &[Vec<usize>],
            st: &mut State,
            sccs: &mut Vec<Vec<usize>>,
        ) {
            st.idx[v] = st.index;
            st.low[v] = st.index;
            st.index += 1;
            st.stack.push(v);
            st.on_stack[v] = true;
            for &w in &adj[v] {
                if st.idx[w] == -1 {
                    strongconnect(w, adj, st, sccs);
                    st.low[v] = st.low[v].min(st.low[w]);
                } else if st.on_stack[w] {
                    st.low[v] = st.low[v].min(st.idx[w]);
                }
            }
            if st.low[v] == st.idx[v] {
                let mut comp = Vec::new();
                loop {
                    let w = st.stack.pop().unwrap();
                    st.on_stack[w] = false;
                    comp.push(w);
                    if w == v {
                        break;
                    }
                }
                sccs.push(comp);
            }
        }
        for v in 0..n {
            if st.idx[v] == -1 {
                strongconnect(v, &adj, &mut st, &mut sccs);
            }
        }

        let mut cycles = Vec::new();
        for comp in sccs {
            let members: BTreeSet<usize> = comp.iter().copied().collect();
            let cyclic = comp.len() > 1
                || comp
                    .first()
                    .is_some_and(|&v| adj[v].contains(&v));
            if !cyclic {
                continue;
            }
            // Reconstruct one concrete cycle path starting at the
            // lexicographically smallest member.
            let start = *comp
                .iter()
                .min_by_key(|&&v| &self.nodes[v].id)
                .unwrap();
            if let Some(path) = find_cycle_path(start, &adj, &members) {
                cycles.push(path.into_iter().map(|v| self.nodes[v].id.clone()).collect());
            } else {
                let mut ids: Vec<String> = comp.iter().map(|&v| self.nodes[v].id.clone()).collect();
                ids.sort();
                cycles.push(ids);
            }
        }
        cycles.sort();
        cycles
    }

    fn to_selection(&self, assignment: &[usize]) -> BTreeMap<String, Selection> {
        let mut map = BTreeMap::new();
        for (i, &alt) in assignment.iter().enumerate() {
            let info = &self.nodes[i].alts[alt];
            map.insert(
                self.nodes[i].id.clone(),
                Selection {
                    expression: conjunction_text(&info.terms),
                    terms: info.terms.iter().map(|t| t.render()).collect(),
                },
            );
        }
        map
    }

    fn total_alternatives(&self) -> u128 {
        self.nodes
            .iter()
            .map(|n| n.alts.len() as u128)
            .product::<u128>()
            .max(1)
    }

    fn effective_policy_json(&self) -> Value {
        let p = &self.policy;
        json!({
            "allowedLicenses": p.allowed.iter().cloned().collect::<Vec<_>>(),
            "copyleft": p.copyleft.iter()
                .map(|(k, v)| json!({"license": k, "copyleft": v.as_str()}))
                .collect::<Vec<_>>(),
            "proprietary": p.proprietary.iter().cloned().collect::<Vec<_>>(),
            "incompatiblePairs": p.incompatible.iter()
                .map(|(a, b)| [a, b])
                .collect::<Vec<_>>(),
            "exceptions": p.exceptions.iter()
                .map(|(k, v)| json!({"exception": k,
                    "effect": match v {
                        crate::policy::ExceptionEffect::Clear => "clear",
                        crate::policy::ExceptionEffect::DowngradeWeak => "downgrade-weak",
                        crate::policy::ExceptionEffect::Keep => "keep",
                    }}))
                .collect::<Vec<_>>(),
            "unknownLicensesAllowed": p.unknown_licenses_allowed,
            "unknownExceptionsAllowed": p.unknown_exceptions_allowed,
            "checkDynamicLinks": p.check_dynamic_links,
        })
    }

    fn run(&self, enumerate: Option<u64>) -> EvaluateResponse {
        let cycles = self.cycles();

        // A package whose every alternative fails the allow matrix can never
        // be selected: report it deterministically without searching.
        let dead: Vec<usize> = (0..self.nodes.len())
            .filter(|&i| self.nodes[i].alts.is_empty())
            .collect();
        if !dead.is_empty() {
            let mut unavoidable = Vec::new();
            for &i in &dead {
                for (alt_text, reason) in &self.nodes[i].invalid_alts {
                    unavoidable.push(Violation {
                        kind: "not-allowed",
                        message: format!(
                            "package {}: every possible choice is rejected ({alt_text}: {reason})",
                            self.nodes[i].id
                        ),
                        path: vec![self.nodes[i].id.clone()],
                        edges: vec![],
                        terms: vec![alt_text.clone()],
                    });
                }
            }
            return EvaluateResponse {
                satisfiable: false,
                selection: None,
                violations: vec![],
                unavoidable_violations: unavoidable,
                cycles,
                assignments_explored: 0,
                enumeration: None,
                effective_policy: self.effective_policy_json(),
                disclaimer: DISCLAIMER.to_string(),
            };
        }

        let (sat, explored_sat) = self.find_satisfying();
        if let Some(assignment) = sat {
            let selection = self.to_selection(&assignment);
            let enumeration = if let Some(limit) = enumerate {
                let (selections, truncated, _) =
                    self.enumerate_all(Some(limit.saturating_add(1)), true);
                let complete = !truncated;
                let taken = if truncated { limit as usize } else { selections.len() };
                Some(EnumerationReport {
                    total_alternatives: self.total_alternatives(),
                    complete,
                    satisfying_selections: selections[..taken.min(selections.len())]
                        .iter()
                        .map(|a| self.to_selection(a))
                        .collect(),
                    truncated_at: truncated.then_some(limit),
                })
            } else {
                None
            };
            EvaluateResponse {
                satisfiable: true,
                selection: Some(selection),
                violations: vec![],
                unavoidable_violations: vec![],
                cycles,
                assignments_explored: explored_sat,
                enumeration,
                effective_policy: self.effective_policy_json(),
                disclaimer: DISCLAIMER.to_string(),
            }
        } else {
            let (unavoidable, explored_enum) = self.unavoidable_violations();
            // Unavoidable set can in theory be empty with the filtered
            // domains (e.g. complex interaction); surface a generic conflict.
            let unavoidable = if unavoidable.is_empty() {
                vec![Violation {
                    kind: "incompatible-licenses",
                    message:
                        "no selection satisfies the policy, but no single violation is common to \
                         every assignment (interacting constraints)"
                            .to_string(),
                    path: vec![],
                    edges: vec![],
                    terms: vec![],
                }]
            } else {
                unavoidable
            };
            EvaluateResponse {
                satisfiable: false,
                selection: None,
                violations: vec![],
                unavoidable_violations: unavoidable,
                cycles,
                assignments_explored: explored_enum.max(explored_sat),
                enumeration: None,
                effective_policy: self.effective_policy_json(),
                disclaimer: DISCLAIMER.to_string(),
            }
        }
    }
}

/// DFS along edges staying inside the SCC, returning a path `start..=start`
/// that closes a cycle.
fn find_cycle_path(start: usize, adj: &[Vec<usize>], members: &BTreeSet<usize>) -> Option<Vec<usize>> {
    let mut stack = vec![(start, 0usize)];
    let mut path = vec![start];
    let mut on_path = BTreeSet::from([start]);
    while let Some(&(v, ni)) = stack.last() {
        let next = adj[v].iter().copied().enumerate().find(|(i, w)| {
            *i >= ni && members.contains(w)
        });
        match next {
            Some((i, w)) => {
                stack.last_mut().unwrap().1 = i + 1;
                if w == start {
                    path.push(start);
                    return Some(path);
                }
                if on_path.insert(w) {
                    path.push(w);
                    stack.push((w, 0));
                }
            }
            None => {
                stack.pop();
                if let Some(v) = path.pop() {
                    on_path.remove(&v);
                }
            }
        }
    }
    None
}

fn dedup_violations(mut violations: Vec<Violation>) -> Vec<Violation> {
    let mut seen = BTreeSet::new();
    violations.retain(|v| seen.insert(v.key()));
    violations
}

const DISCLAIMER: &str =
    "Rule-engine output only: a mechanical heuristic over the supplied SPDX subset and policy \
     matrix. It is NOT legal advice and makes no legal conclusion about license compliance.";

/// Public entry point used by the HTTP layer.
pub fn evaluate(req: &EvaluateRequest) -> Result<EvaluateResponse, String> {
    let engine = Engine::build(req)?;
    Ok(engine.run(req.enumerate))
}

/// Parse-only helper backing `POST /parse`.
pub fn describe_expression(input: &str) -> Result<Value, String> {
    let expr = parse(input)?;
    let dnf = to_dnf(&expr);
    Ok(json!({
        "input": input,
        "ast": render_ast(&expr),
        "dnfAlternatives": dnf.iter().map(spdx::conjunction_text).collect::<Vec<_>>(),
    }))
}

fn render_ast(expr: &Expr) -> Value {
    match expr {
        Expr::Term(t) => json!({
            "node": "term",
            "license": t.license,
            "exception": t.exception,
        }),
        Expr::And(a, b) => json!({"node": "AND", "children": [render_ast(a), render_ast(b)]}),
        Expr::Or(a, b) => json!({"node": "OR", "children": [render_ast(a), render_ast(b)]}),
    }
}
