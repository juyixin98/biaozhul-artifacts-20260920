//! Backtracking dependency solver.
//!
//! The search is a classic CSP:
//! - variables: constrained package names
//! - domains: published versions satisfying every accumulated constraint
//! - value ordering: a fixed per-package preference (highest/lowest/pin)
//! - variable ordering: MRV (fewest feasible candidates first) so that
//!   conflicts surface as close to their cause as possible
//!
//! On failure the solver returns a conflict chain: every constraint imposed on
//! the dead package, every candidate tried for its parent frames and why, and
//! nested subtree conflicts. It never reports "unsolvable" after a single
//! greedy pass — every candidate combination consistent with the ordering is
//! explored before giving up.

use std::collections::{BTreeMap, BTreeSet};

use semver::{Prerelease, Version};
use crate::model::{
    ConflictView, DefaultPref, Dependency, PackageVersion, Preference,
    RejectionView, RootRequirement, SolveRequest, SolveStats, TraceStep,
};
use crate::platform::{Platform, Target};
use crate::semver_range::Req;

#[derive(Debug, Clone)]
struct Origin {
    /// Requiring package name, or `"root"` for application requirements.
    from: String,
    version: Option<Version>,
    feature: Option<String>,
}

#[derive(Debug, Clone)]
struct Constraint {
    req: Req,
    origin: Origin,
    optional: bool,
    target_active: bool,
    /// If this constraint was imposed against an already-selected package,
    /// remember that selection for explanation purposes.
    against: Option<(String, Version)>,
}

#[derive(Default, Debug, Clone)]
struct Counts {
    assignments: usize,
    backtracks: usize,
    nodes_explored: usize,
}

#[derive(Clone, Default)]
struct SearchState {
    assignment: BTreeMap<String, Version>,
    constraints: BTreeMap<String, Vec<Constraint>>,
}

/// Why a candidate version was rejected at a decision frame.
enum Attempt {
    /// The candidate's own dependency edge contradicted an already-made choice.
    Immediate { version: Version, by: TraceStep },
    /// A deeper subtree failed under this candidate.
    Subtree { conflict: ConflictView },
}

pub struct Solver<'a> {
    req: &'a SolveRequest,
    platform: Option<&'a Platform>,
    counts: Counts,
    notes: BTreeSet<String>,
}

impl<'a> Solver<'a> {
    pub fn new(req: &'a SolveRequest) -> Self {
        Solver {
            req,
            platform: req.platform.as_ref(),
            counts: Counts::default(),
            notes: BTreeSet::new(),
        }
    }

    pub fn solve(&mut self) -> Result<Solution, ConflictView> {
        let mut state = SearchState::default();

        // Fixed preferences enter as hard root-origin constraints.
        for (name, pref) in &self.req.preferences {
            if let Preference::Pin { version } = pref {
                let req = Req::parse(&format!("={version}")).expect("pin renders to valid req");
                state.constraints.entry(name.clone()).or_default().push(Constraint {
                    req,
                    origin: Origin { from: "root".into(), version: None, feature: Some("pin".into()) },
                    optional: false,
                    target_active: true,
                    against: None,
                });
            }
        }

        for RootRequirement { name, req } in &self.req.requirements {
            state.constraints.entry(name.clone()).or_default().push(Constraint {
                req: req.clone(),
                origin: Origin { from: "root".into(), version: None, feature: None },
                optional: false,
                target_active: true,
                against: None,
            });
        }

        let assignment = self.search(state)?;
        let edges = self.active_edges(&assignment);
        let cycles = find_cycles(&edges);
        Ok(Solution { assignment, cycles, edges })
    }

    pub fn take_notes(&mut self) -> Vec<String> {
        std::mem::take(&mut self.notes).into_iter().collect()
    }

    fn search(&mut self, state: SearchState) -> Result<BTreeMap<String, Version>, ConflictView> {
        self.counts.nodes_explored += 1;

        // Choose the next unassigned constrained package (MRV).
        let Some((name, candidates)) = self.pick_variable(&state) else {
            // No constrained package left unassigned: complete assignment.
            return Ok(state.assignment);
        };

        if candidates.is_empty() {
            return Err(self.dead_end(&name, &state));
        }

        let mut attempts: Vec<Attempt> = Vec::new();

        for cand in candidates {
            self.counts.assignments += 1;
            let mut branch = state.clone();
            branch.assignment.insert(name.clone(), cand.clone());

            let blocked = self.impose_edges(&name, &cand, &mut branch);
            if let Some(by) = blocked {
                self.counts.backtracks += 1;
                attempts.push(Attempt::Immediate { version: cand, by });
                continue;
            }

            match self.search(branch) {
                Ok(a) => return Ok(a),
                Err(conflict) => {
                    self.counts.backtracks += 1;
                    attempts.push(Attempt::Subtree { conflict });
                }
            }
        }

        Err(self.frame_conflict(&name, &state, attempts))
    }

    /// MRV: constrained + unassigned package with the smallest feasible
    /// domain, tie-broken by name for determinism.
    fn pick_variable(&self, state: &SearchState) -> Option<(String, Vec<Version>)> {
        let mut best: Option<(String, Vec<Version>)> = None;
        for (name, cons) in &state.constraints {
            if state.assignment.contains_key(name) {
                continue;
            }
            let mut cands = self.feasible(name, cons);
            self.order_candidates(name, &mut cands);
            match &best {
                None => best = Some((name.clone(), cands)),
                Some((bn, bc)) => {
                    if cands.len() < bc.len() || (cands.len() == bc.len() && name < bn) {
                        best = Some((name.clone(), cands));
                    }
                }
            }
        }
        best
    }

    fn feasible(&self, name: &str, cons: &[Constraint]) -> Vec<Version> {
        let Some(versions) = self.req.registry.packages.get(name) else {
            return Vec::new();
        };
        versions
            .iter()
            .filter(|pv| !pv.yanked)
            .map(|pv| pv.version.clone())
            .filter(|v| {
                let allow_prerelease = self.req.allow_prerelease
                    || v.pre == Prerelease::EMPTY
                    || cons.iter().any(|c| c.req.allows_prerelease_of(v));
                cons.iter().all(|c| c.req.matches(v, allow_prerelease))
            })
            .collect()
    }

    fn order_candidates(&self, name: &str, cands: &mut [Version]) {
        let pref = self.req.preferences.get(name);
        let lowest = matches!(pref, Some(Preference::Lowest))
            || (pref.is_none() && matches!(self.req.default_preference, DefaultPref::Lowest));
        if lowest {
            cands.sort();
        } else {
            cands.sort_by(|a, b| b.cmp(a));
        }
    }

    /// Insert the active dependency edges of `name@version` into the state.
    /// Returns a rejecting trace step if an edge lands on an already-selected
    /// package at an incompatible version.
    fn impose_edges(&mut self, name: &str, version: &Version, state: &mut SearchState) -> Option<TraceStep> {
        let enabled = self.req.features.get(name).cloned().unwrap_or_default();
        let pv = self
            .req
            .registry
            .packages
            .get(name)
            .and_then(|vs| vs.iter().find(|pv| &pv.version == version))
            .expect("selected version exists in registry");

        let mut known_features: BTreeSet<String> = BTreeSet::new();
        for dep in &pv.dependencies {
            known_features.insert(dep.feature_name());
        }
        for f in &enabled {
            if !known_features.contains(f) {
                self.notes.insert(format!(
                    "package {name} has no feature {f:?}; ignored"
                ));
            }
        }

        for dep in &pv.dependencies {
            let target_active = dep.target.as_ref().map(|t| self.target_matches(t)).unwrap_or(true);
            if !target_active {
                continue;
            }
            if dep.optional && !enabled.iter().any(|f| f == &dep.feature_name()) {
                continue;
            }
            let entry = state.constraints.entry(dep.name.clone()).or_default();
            let origin = Origin {
                from: name.to_string(),
                version: Some(version.clone()),
                feature: dep.optional.then(|| dep.feature_name()),
            };
            if let Some(existing) = state.assignment.get(&dep.name) {
                let allow = self.req.allow_prerelease
                    || existing.pre == Prerelease::EMPTY
                    || dep.req.allows_prerelease_of(existing);
                if !dep.req.matches(existing, allow) {
                    return Some(trace_step(&dep.req, &origin, dep.optional, true, Some((dep.name.clone(), existing.clone()))));
                }
            }
            entry.push(Constraint {
                req: dep.req.clone(),
                origin,
                optional: dep.optional,
                target_active: true,
                against: state.assignment.get(&dep.name).map(|v| (dep.name.clone(), v.clone())),
            });
        }
        None
    }

    fn target_matches(&self, t: &Target) -> bool {
        match self.platform {
            Some(p) => t.eval(p),
            None => false,
        }
    }

    /// The active edge set of a complete assignment (for cycles + lockfile).
    fn active_edges(&self, assignment: &BTreeMap<String, Version>) -> BTreeMap<String, Vec<ActiveEdge>> {
        let mut out: BTreeMap<String, Vec<ActiveEdge>> = BTreeMap::new();
        for (name, version) in assignment {
            let Some(pv) = self.pv(name, version) else { continue };
            let enabled = self.req.features.get(name).cloned().unwrap_or_default();
            for dep in &pv.dependencies {
                let target_active = dep.target.as_ref().map(|t| self.target_matches(t)).unwrap_or(true);
                if !target_active {
                    continue;
                }
                if dep.optional && !enabled.iter().any(|f| f == &dep.feature_name()) {
                    continue;
                }
                out.entry(name.clone()).or_default().push(ActiveEdge {
                    to: dep.name.clone(),
                    req: dep.req.clone(),
                    optional: dep.optional,
                });
            }
        }
        out
    }

    fn pv(&self, name: &str, version: &Version) -> Option<&PackageVersion> {
        self.req
            .registry
            .packages
            .get(name)
            .and_then(|vs| vs.iter().find(|pv| &pv.version == version))
    }

    /// Conflict when a package's feasible domain is already empty: list the
    /// complete constraint chain imposed on it.
    fn dead_end(&self, name: &str, state: &SearchState) -> ConflictView {
        let cons = state.constraints.get(name).cloned().unwrap_or_default();
        let trace = cons.iter().map(|c| trace_step(&c.req, &c.origin, c.optional, c.target_active, c.against.clone())).collect();
        ConflictView { package: name.to_string(), trace, rejections: vec![], nested: vec![] }
    }

    /// Conflict after trying every candidate at a frame.
    fn frame_conflict(&self, name: &str, state: &SearchState, attempts: Vec<Attempt>) -> ConflictView {
        let cons = state.constraints.get(name).cloned().unwrap_or_default();
        let trace = cons.iter().map(|c| trace_step(&c.req, &c.origin, c.optional, c.target_active, c.against.clone())).collect();

        let mut rejections = Vec::new();
        let mut nested = Vec::new();
        let mut seen: BTreeSet<String> = BTreeSet::new();
        for a in attempts {
            match a {
                Attempt::Immediate { version, by } => rejections.push(RejectionView { version, rejected_by: by }),
                Attempt::Subtree { conflict } => {
                    let key = serde_json::to_string(&conflict).unwrap_or_default();
                    if seen.insert(key) {
                        nested.push(conflict);
                    }
                }
            }
        }
        ConflictView { package: name.to_string(), trace, rejections, nested }
    }

    pub fn stats(&self) -> SolveStats {
        SolveStats {
            assignments: self.counts.assignments,
            backtracks: self.counts.backtracks,
            nodes_explored: self.counts.nodes_explored,
            elapsed_ms: 0,
            oracle: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ActiveEdge {
    pub to: String,
    pub req: Req,
    /// Whether the edge was behind an enabled optional feature.
    #[allow(dead_code)]
    pub optional: bool,
}

#[derive(Debug, Clone)]
pub struct Solution {
    pub assignment: BTreeMap<String, Version>,
    pub cycles: Vec<Vec<String>>,
    pub edges: BTreeMap<String, Vec<ActiveEdge>>,
}

/// Everything the service layer needs from one solve run.
pub struct SolveOutcome {
    pub result: Result<Solution, ConflictView>,
    pub stats: SolveStats,
    pub notes: Vec<String>,
}

pub fn run(req: &SolveRequest) -> SolveOutcome {
    let started = std::time::Instant::now();
    let mut solver = Solver::new(req);
    let result = solver.solve();
    let mut stats = solver.stats();
    stats.elapsed_ms = started.elapsed().as_millis();
    let notes = solver.take_notes();
    SolveOutcome { result, stats, notes }
}

fn trace_step(
    req: &Req,
    origin: &Origin,
    optional: bool,
    target_active: bool,
    against: Option<(String, Version)>,
) -> TraceStep {
    let mut constraint = req.to_string();
    if let Some(f) = &origin.feature {
        constraint = format!("{constraint} (via feature {f:?})");
    }
    if let Some((p, v)) = &against {
        constraint = format!("{constraint}, but {p} is already selected at {v}");
    }
    TraceStep {
        from: origin.from.clone(),
        version: origin.version.clone(),
        constraint,
        optional,
        target_active,
    }
}

/// Whether a dependency edge is active under a request (shared by solver and
/// the brute-force oracle).
pub fn edge_active(req: &SolveRequest, dependent: &str, _pv: &PackageVersion, dep: &Dependency) -> bool {
    let target_ok = dep
        .target
        .as_ref()
        .map(|t| req.platform.as_ref().map(|p| t.eval(p)).unwrap_or(false))
        .unwrap_or(true);
    if !target_ok {
        return false;
    }
    if !dep.optional {
        return true;
    }
    req.features
        .get(dependent)
        .map(|fs| fs.iter().any(|f| f == &dep.feature_name()))
        .unwrap_or(false)
}

/// Enumerate all simple cycles. Each cycle is returned once, rotated so its
/// lexicographically smallest node comes first (self-loops as `[a]`).
fn find_cycles(edges: &BTreeMap<String, Vec<ActiveEdge>>) -> Vec<Vec<String>> {
    let nodes: BTreeSet<&String> = edges
        .iter()
        .flat_map(|(f, ts)| std::iter::once(f).chain(ts.iter().map(|e| &e.to)))
        .collect();
    let ordered: Vec<&String> = nodes.into_iter().collect();

    let mut cycles: BTreeSet<Vec<String>> = BTreeSet::new();
    for (i, start) in ordered.iter().enumerate() {
        // Only consider nodes at position >= i on the path.
        let allowed: BTreeSet<&String> = ordered[i..].iter().copied().collect();
        let mut stack = vec![(*start).clone()];
        let mut on_stack: BTreeSet<String> = BTreeSet::new();
        on_stack.insert((*start).clone());
        dfs_cycles(
            start,
            start,
            edges,
            &allowed,
            &mut stack,
            &mut on_stack,
            &mut cycles,
        );
    }
    cycles.into_iter().collect()
}

#[allow(clippy::too_many_arguments)]
fn dfs_cycles(
    start: &str,
    cur: &str,
    edges: &BTreeMap<String, Vec<ActiveEdge>>,
    allowed: &BTreeSet<&String>,
    stack: &mut Vec<String>,
    on_stack: &mut BTreeSet<String>,
    cycles: &mut BTreeSet<Vec<String>>,
) {
    if let Some(out) = edges.get(cur) {
        for e in out {
            if e.to == start {
                if stack.len() == 1 {
                    // self-loop only when edge points to itself
                    if cur == start {
                        cycles.insert(vec![start.to_string()]);
                    }
                } else {
                    cycles.insert(stack.clone());
                }
                continue;
            }
            if !allowed.contains(&e.to) || on_stack.contains(&e.to) {
                continue;
            }
            stack.push(e.to.clone());
            on_stack.insert(e.to.clone());
            dfs_cycles(start, &e.to, edges, allowed, stack, on_stack, cycles);
            on_stack.remove(&e.to);
            stack.pop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cycle_finding() {
        let mut edges: BTreeMap<String, Vec<ActiveEdge>> = BTreeMap::new();
        let mk = |to: &str| ActiveEdge { to: to.into(), req: Req::parse("*").unwrap(), optional: false };
        edges.insert("a".into(), vec![mk("b")]);
        edges.insert("b".into(), vec![mk("c"), mk("a")]);
        edges.insert("c".into(), vec![mk("a")]);
        let cycles = find_cycles(&edges);
        assert_eq!(cycles, vec![vec!["a".to_string(), "b".to_string()], vec!["a".to_string(), "b".to_string(), "c".to_string()]]);

        edges.clear();
        edges.insert("a".into(), vec![mk("a")]);
        assert_eq!(find_cycles(&edges), vec![vec!["a".to_string()]]);
    }
}
