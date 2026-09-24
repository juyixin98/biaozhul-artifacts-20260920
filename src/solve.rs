// Rule engine + solvers.
//
// For each package the SPDX expression gives OR-alternatives of AND-terms.
// A solution picks one term per package. A picked assignment is valid iff:
//
//   1. every chosen atom is known (unless the policy allows unknowns),
//      carries a recognized exception if any, and is permitted by the
//      project allow matrix for the package's incoming link contexts;
//
//   2. all atoms chosen within one package (an AND term) are pairwise
//      compatible under the copyleft matrix;
//
//   3. for every chosen atom with (weak|strong) copyleft, every package in
//      reverse propagation reach (consumer of the dependency, following
//      allowed link types) has a chosen atom compatible with the
//      obligation. Exceptions on an atom suppress *its* emitted
//      obligations only.
//
// Two solvers implement the same spec:
//   * `backtrack` — MRV backtracking with monotone propagation pruning;
//   * `enumerate` — exhaustive enumeration used as an oracle for small cases.

use std::collections::HashSet;

use crate::graph::Graph;
use crate::model::{
    ConflictView, EdgeHop, ExhaustiveView, Link, ObligationView, Strength, TermView,
};
use crate::policy::{AtomStaticError, CopyleftClass, ResolvedPolicy};
use crate::spdx::{Atom, SpdxExpr, Term};

#[derive(Debug, Clone)]
pub struct PreparedNode {
    pub id: String,
    pub spdx_text: String,
    pub expr: SpdxExpr,
    pub contexts: Vec<Link>,
    /// One entry per DNF term: the term plus the reason it is statically
    /// infeasible (if any). Feasible indices form the solver domain.
    pub term_status: Vec<Result<(), StaticTermError>>,
}

#[derive(Debug, Clone)]
pub enum StaticTermError {
    Atom {
        atom: Atom,
        error: AtomStaticError,
    },
}

#[derive(Debug, Clone)]
pub struct ActiveObligation {
    pub source: usize,
    pub license: String,
    pub strength: Strength,
}

#[derive(Debug, Clone)]
pub struct Solution {
    pub chosen_terms: Vec<usize>,
    pub obligations: Vec<ActiveObligation>,
}

pub struct Prepared {
    pub nodes: Vec<PreparedNode>,
}

pub fn prepare(
    ids: &[String],
    spdx_texts: &[String],
    exprs: Vec<SpdxExpr>,
    graph: &Graph,
    policy: &ResolvedPolicy,
) -> Result<Prepared, (usize, String)> {
    let mut nodes = Vec::with_capacity(ids.len());
    for (i, expr) in exprs.into_iter().enumerate() {
        let contexts = graph.incoming_contexts(i);
        let term_status = expr
            .dnf
            .iter()
            .map(|t| {
                for atom in &t.atoms {
                    if let Err(error) = policy.atom_static_ok(atom, &contexts) {
                        return Err(StaticTermError::Atom {
                            atom: atom.clone(),
                            error,
                        });
                    }
                }
                Ok(())
            })
            .collect();
        nodes.push(PreparedNode {
            id: ids[i].clone(),
            spdx_text: spdx_texts[i].clone(),
            expr,
            contexts,
            term_status,
        });
    }
    Ok(Prepared { nodes })
}

fn domains(prep: &Prepared) -> Vec<Vec<usize>> {
    prep.nodes
        .iter()
        .map(|n| {
            n.term_status
                .iter()
                .enumerate()
                .filter_map(|(ti, st)| st.as_ref().ok().map(|_| ti))
                .collect()
        })
        .collect()
}

fn chosen_atoms(prep: &Prepared, node: usize, term: usize) -> &[Atom] {
    &prep.nodes[node].expr.dnf[term].atoms
}

/// Copyleft classification of an atom: exceptions suppress emissions,
/// unknown licenses allowed by policy are treated as non-copyleft.
fn emits(atom: &Atom, policy: &ResolvedPolicy) -> Option<(Strength, String)> {
    if atom.exception.is_some() {
        return None;
    }
    match policy.classify(&atom.license) {
        CopyleftClass::Weak => Some((Strength::Weak, atom.license.clone())),
        CopyleftClass::Strong => Some((Strength::Strong, atom.license.clone())),
        CopyleftClass::None => None,
    }
}

/// All atoms within a chosen term must be pairwise compatible with any
/// copyleft obligations present in that same term.
fn term_internal_ok(term: &Term, policy: &ResolvedPolicy) -> bool {
    let obligations: Vec<(Strength, String)> = term
        .atoms
        .iter()
        .filter_map(|a| emits(a, policy))
        .collect();
    // The origin package itself is always in scope of its own obligation,
    // even when that strength propagates on no link type.
    for (_, obl_lic) in &obligations {
        for target_atom in &term.atoms {
            if !policy.compatible(obl_lic, &target_atom.license) {
                return false;
            }
        }
    }
    true
}

#[derive(Debug, Clone)]
struct Violation {
    source: usize,
    source_license: String,
    strength: Strength,
    target: usize,
    target_license: String,
    target_has_exception: bool,
    path: Vec<EdgeHop>,
}

/// Evaluate a complete assignment; returns all copyleft violations
/// deterministically ordered, or the set of active obligations.
fn evaluate(
    assignment: &[usize],
    prep: &Prepared,
    graph: &Graph,
    policy: &ResolvedPolicy,
) -> Result<Vec<ActiveObligation>, Vec<Violation>> {
    // 1. internal AND-term compatibility
    for (n, &ti) in assignment.iter().enumerate() {
        if !term_internal_ok(&prep.nodes[n].expr.dnf[ti], policy) {
            let v = internal_violation(n, ti, prep, policy);
            return Err(vec![v]);
        }
    }
    // 2. collect emitted obligations from chosen non-exception atoms
    let mut sources: Vec<(usize, Strength, String)> = Vec::new();
    for (n, &ti) in assignment.iter().enumerate() {
        for atom in chosen_atoms(prep, n, ti) {
            if let Some((strength, lic)) = emits(atom, policy) {
                sources.push((n, strength, lic));
            }
        }
    }
    let mut violations = Vec::new();
    let mut active = Vec::new();
    for (src, strength, lic) in &sources {
        let links = policy.propagation_links(*strength);
        let reach = Graph::reverse_reachable(*src, links, graph);
        active.push(ActiveObligation {
            source: *src,
            license: lic.clone(),
            strength: *strength,
        });
        for &t in &reach {
            for atom in chosen_atoms(prep, t, assignment[t]) {
                if !policy.compatible(lic, &atom.license) {
                    let path = Graph::obligation_path(*src, t, links, graph).unwrap_or_default();
                    violations.push(Violation {
                        source: *src,
                        source_license: lic.clone(),
                        strength: *strength,
                        target: t,
                        target_license: atom.license.clone(),
                        target_has_exception: atom.exception.is_some(),
                        path,
                    });
                }
            }
        }
    }
    if violations.is_empty() {
        Ok(active)
    } else {
        violations.sort_by(violation_order);
        violations.dedup_by(|a, b| {
            a.source == b.source
                && a.target == b.target
                && a.strength == b.strength
                && a.source_license == b.source_license
                && a.target_license == b.target_license
        });
        Err(violations)
    }
}

fn internal_violation(n: usize, ti: usize, prep: &Prepared, policy: &ResolvedPolicy) -> Violation {
    let term = &prep.nodes[n].expr.dnf[ti];
    // Find the first (obligation, atom) incompatible pair within the term.
    for obl_atom in &term.atoms {
        if let Some((strength, obl_lic)) = emits(obl_atom, policy) {
            for tgt_atom in &term.atoms {
                if !policy.compatible(&obl_lic, &tgt_atom.license) {
                    return Violation {
                        source: n,
                        source_license: obl_lic,
                        strength,
                        target: n,
                        target_license: tgt_atom.license.clone(),
                        target_has_exception: tgt_atom.exception.is_some(),
                        path: Vec::new(),
                    };
                }
            }
        }
    }
    // Defensive: term_internal_ok was false, so a pair must exist.
    let lic = term.atoms[0].license.clone();
    Violation {
        source: n,
        source_license: lic.clone(),
        strength: Strength::Strong,
        target: n,
        target_license: lic,
        target_has_exception: term.atoms[0].exception.is_some(),
        path: Vec::new(),
    }
}

fn violation_order(a: &Violation, b: &Violation) -> std::cmp::Ordering {
    a.source
        .cmp(&b.source)
        .then_with(|| a.target.cmp(&b.target))
        .then_with(|| {
            a.strength
                .partial_cmp(&b.strength)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .then_with(|| a.source_license.cmp(&b.source_license))
        .then_with(|| a.target_license.cmp(&b.target_license))
}

// ---------------------------------------------------------------------------
// Backtracking solver
// ---------------------------------------------------------------------------

pub fn backtrack(prep: &Prepared, graph: &Graph, policy: &ResolvedPolicy) -> Option<Solution> {
    // Per-node domain: terms that survive static allow-matrix checks and
    // whose AND conjunction is self-compatible.
    let mut doms: Vec<Vec<usize>> = prep
        .nodes
        .iter()
        .map(|n| {
            n.term_status
                .iter()
                .enumerate()
                .filter_map(|(ti, st)| {
                    if st.is_ok() && term_internal_ok(&n.expr.dnf[ti], policy) {
                        Some(ti)
                    } else {
                        None
                    }
                })
                .collect()
        })
        .collect();
    for d in doms.iter_mut() {
        d.sort_unstable();
        d.dedup();
    }
    if doms.iter().any(Vec::is_empty) {
        return None;
    }

    let n = prep.nodes.len();
    let mut assignment = vec![usize::MAX; n];

    fn partial_violations(
        assigned: &[usize],
        prep: &Prepared,
        graph: &Graph,
        policy: &ResolvedPolicy,
    ) -> Vec<Violation> {
        // obligations emitted so far
        let mut emitted: Vec<(usize, Strength, String)> = Vec::new();
        for (n, &ti) in assigned.iter().enumerate() {
            if ti == usize::MAX {
                continue;
            }
            for atom in chosen_atoms(prep, n, ti) {
                if let Some((s, lic)) = emits(atom, policy) {
                    emitted.push((n, s, lic));
                }
            }
        }
        let mut out = Vec::new();
        for (src, strength, lic) in emitted {
            let links = policy.propagation_links(strength);
            let reach = Graph::reverse_reachable(src, links, graph);
            for &t in &reach {
                if assigned[t] == usize::MAX {
                    continue; // not yet decided: may become compatible
                }
                for atom in chosen_atoms(prep, t, assigned[t]) {
                    if !policy.compatible(&lic, &atom.license) {
                        let path =
                            Graph::obligation_path(src, t, links, graph).unwrap_or_default();
                        out.push(Violation {
                            source: src,
                            source_license: lic.clone(),
                            strength,
                            target: t,
                            target_license: atom.license.clone(),
                            target_has_exception: atom.exception.is_some(),
                            path,
                        });
                    }
                }
            }
        }
        out
    }

    fn search(
        next: usize,
        assignment: &mut [usize],
        doms: &[Vec<usize>],
        prep: &Prepared,
        graph: &Graph,
        policy: &ResolvedPolicy,
    ) -> bool {
        let n = assignment.len();
        if next == n {
            return true;
        }
        // MRV: pick the unassigned node with the smallest remaining domain.
        let mut node = usize::MAX;
        let mut best = usize::MAX;
        for i in 0..n {
            if assignment[i] != usize::MAX {
                continue;
            }
            let mut feasible_terms = 0usize;
            'terms: for &ti in &doms[i] {
                // tentatively assign and check only violations both ends fixed
                assignment[i] = ti;
                let vs = partial_violations(assignment, prep, graph, policy);
                assignment[i] = usize::MAX;
                for v in vs {
                    if v.source == i || v.target == i {
                        continue 'terms; // hard violation involving the pick
                    }
                }
                feasible_terms += 1;
            }
            if feasible_terms == 0 {
                return false;
            }
            if feasible_terms < best {
                best = feasible_terms;
                node = i;
            }
        }

        for &ti in &doms[node] {
            assignment[node] = ti;
            let vs = partial_violations(assignment, prep, graph, policy);
            // A violation where both endpoints are assigned is final.
            let hard = vs
                .iter()
                .any(|v| assignment[v.source] != usize::MAX && assignment[v.target] != usize::MAX);
            if !hard && search(next + 1, assignment, doms, prep, graph, policy) {
                return true;
            }
            assignment[node] = usize::MAX;
        }
        false
    }

    if !search(0, &mut assignment, &doms, prep, graph, policy) {
        return None;
    }
    match evaluate(&assignment, prep, graph, policy) {
        Ok(obligations) => Some(Solution {
            chosen_terms: assignment,
            obligations,
        }),
        Err(_) => None, // defensive: pruning guarantees this cannot happen
    }
}

// ---------------------------------------------------------------------------
// Exhaustive oracle
// ---------------------------------------------------------------------------

pub struct Enumeration {
    pub feasible: usize,
    pub total_terms_product: usize,
    pub capped: bool,
}

/// Enumerate every assignment over statically+internally feasible terms and
/// count those fully satisfying the policy.
pub fn enumerate(
    prep: &Prepared,
    graph: &Graph,
    policy: &ResolvedPolicy,
    cap: usize,
) -> Enumeration {
    let doms: Vec<Vec<usize>> = prep
        .nodes
        .iter()
        .map(|n| {
            n.term_status
                .iter()
                .enumerate()
                .filter_map(|(ti, st)| {
                    if st.is_ok() && term_internal_ok(&n.expr.dnf[ti], policy) {
                        Some(ti)
                    } else {
                        None
                    }
                })
                .collect()
        })
        .collect();
    let total_terms_product: usize = doms.iter().map(|d| d.len().max(1)).product();
    if doms.iter().any(Vec::is_empty) {
        return Enumeration {
            feasible: 0,
            total_terms_product,
            capped: false,
        };
    }
    let n = doms.len();
    let mut assignment = vec![0usize; n];
    let mut feasible = 0usize;
    let mut visited = 0usize;
    let mut capped = false;
    if n == 0 {
        return Enumeration {
            feasible: 1,
            total_terms_product: 1,
            capped,
        };
    }
    loop {
        if visited >= cap {
            capped = true;
            break;
        }
        visited += 1;
        if evaluate(&assignment, prep, graph, policy).is_ok() {
            feasible += 1;
        }
        // mixed-radix increment
        let mut k = n;
        loop {
            if k == 0 {
                return Enumeration {
                    feasible,
                    total_terms_product,
                    capped,
                };
            }
            k -= 1;
            assignment[k] += 1;
            if assignment[k] < doms[k].len() {
                break;
            }
            assignment[k] = 0;
        }
    }
    Enumeration {
        feasible,
        total_terms_product,
        capped,
    }
}

// ---------------------------------------------------------------------------
// Conflict / result rendering
// ---------------------------------------------------------------------------

pub fn term_view(term: &Term, feasible: bool) -> TermView {
    TermView {
        expression: term
            .atoms
            .iter()
            .map(|a| a.display())
            .collect::<Vec<_>>()
            .join(" AND "),
        atoms: term.atoms.iter().map(|a| a.display()).collect(),
        feasible,
    }
}

pub fn static_conflicts(prep: &Prepared) -> Vec<ConflictView> {
    let mut out = Vec::new();
    for node in &prep.nodes {
        let mut seen = HashSet::new();
        for st in &node.term_status {
            if let Err(StaticTermError::Atom { atom, error }) = st {
                let key = format!("{}|{:?}", atom.display(), error);
                if !seen.insert(key) {
                    continue;
                }
                let ctx = context_tag(node.contexts.as_slice());
                let (license, detail) = match error {
                    AtomStaticError::Unknown => (
                        atom.license.clone(),
                        "license is not known to the policy and unknown_licenses=reject"
                            .to_string(),
                    ),
                    AtomStaticError::UnrecognizedException => (
                        atom.license.clone(),
                        format!(
                            "exception '{}' is not in the policy's recognized exception list",
                            atom.exception.as_deref().unwrap_or("")
                        ),
                    ),
                    AtomStaticError::Allow(v) => {
                        use crate::policy::AllowViolation::*;
                        let detail = match v {
                            NotInMatrix => {
                                "license is not present in the project allowed-license matrix"
                                    .to_string()
                            }
                            RootContext => {
                                "package has no incoming dependency edge (root context '*') but \
                                 the matrix entry does not include '*'"
                                    .to_string()
                            }
                            EdgeContext(tag) => format!(
                                "package is consumed via a '{}' link but the matrix entry does \
                                 not allow '{}'",
                                tag, tag
                            ),
                        };
                        (atom.license.clone(), detail)
                    }
                };
                out.push(ConflictView::Allow {
                    package: node.id.clone(),
                    license,
                    context: ctx,
                    detail,
                });
            }
        }
    }
    out
}

fn context_tag(contexts: &[Link]) -> String {
    if contexts.is_empty() {
        "*".to_string()
    } else {
        contexts
            .iter()
            .map(|l| match l {
                Link::Static => "static",
                Link::Dynamic => "dynamic",
            })
            .collect::<Vec<_>>()
            .join("+")
    }
}

/// Build a concrete copyleft conflict witness when no solution exists even
/// though each package has at least one allow-matrix-feasible term:
/// greedily pick each node's first feasible term and report the first
/// blocking violation.
pub fn copyleft_conflict_witness(
    prep: &Prepared,
    graph: &Graph,
    policy: &ResolvedPolicy,
) -> Option<ConflictView> {
    let doms = domains(prep);
    if doms.iter().any(Vec::is_empty) {
        return None; // allow-matrix conflicts already explain infeasibility
    }
    // Greedy reference assignment: each node's first internally compatible
    // feasible term. It need not solve; it exists to exhibit a conflict path.
    let assignment: Vec<usize> = (0..prep.nodes.len())
        .map(|i| {
            let n = &prep.nodes[i];
            *doms[i]
                .iter()
                .find(|ti| term_internal_ok(&n.expr.dnf[**ti], policy))
                .unwrap_or(&doms[i][0])
        })
        .collect();

    match evaluate(&assignment, prep, graph, policy) {
        Ok(_) => None, // should not happen: solver says infeasible
        Err(vs) => Some(to_conflict(prep, &vs[0])),
    }
}

fn to_conflict(prep: &Prepared, v: &Violation) -> ConflictView {
    ConflictView::Copyleft {
        source: prep.nodes[v.source].id.clone(),
        source_license: v.source_license.clone(),
        strength: v.strength,
        path: v.path.clone(),
        target: prep.nodes[v.target].id.clone(),
        target_license: v.target_license.clone(),
        target_has_exception: v.target_has_exception,
        detail: format!(
            "{} copyleft obligation of '{}' (strength {}) reaches package '{}' whose chosen \
             license '{}' is not listed in the policy compatibility matrix",
            if v.source == v.target {
                "within-package AND-term"
            } else {
                "propagated"
            },
            v.source_license,
            match v.strength {
                Strength::Weak => "weak",
                Strength::Strong => "strong",
            },
            prep.nodes[v.target].id,
            v.target_license
        ),
    }
}

pub fn to_obligation(prep: &Prepared, o: &ActiveObligation) -> ObligationView {
    ObligationView {
        source: prep.nodes[o.source].id.clone(),
        source_license: o.license.clone(),
        strength: o.strength,
        exception: None,
    }
}

pub fn exhaustive_view(result: &Enumeration, solver_sat: bool, cap: usize) -> ExhaustiveView {
    ExhaustiveView {
        feasible_assignments: result.feasible,
        total_assignments_of_feasible_terms: result.total_terms_product,
        capped: result.capped,
        cap,
        agrees_with_solver: solver_sat == (result.feasible > 0),
    }
}

// Used by tests: which nodes emit in a solved assignment.
pub fn emitted_sources(sol: &Solution) -> &[ActiveObligation] {
    &sol.obligations
}
