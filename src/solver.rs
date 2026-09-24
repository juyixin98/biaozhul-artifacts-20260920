//! Backtracking dependency solver.
//!
//! The solver never declares "no solution" on the first greedy failure:
//! it explores candidate versions in a fixed preference order (highest
//! version first, registry order as tiebreak) and backtracks on conflict,
//! recording the deepest conflict chain seen for the final explanation.

use crate::model::{PackageVersion, Registry, Requirement};
use semver::{Version, VersionReq};
use serde::Serialize;
use std::collections::BTreeMap;

/// Where a constraint came from: the chain of selected packages that
/// introduced it, starting at "<root>".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConstraintSource {
    /// e.g. ["<root>", "a@1.0.0"]
    pub chain: Vec<String>,
    /// the requirement string, e.g. ">=2.0.0"
    pub range: String,
}

#[derive(Debug, Clone, Default)]
struct State {
    /// package name -> chosen version
    selected: BTreeMap<String, Version>,
    /// package name -> all constraints accumulated so far, with provenance
    constraints: BTreeMap<String, Vec<ConstraintSource>>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct ConflictChain {
    pub required_by: Vec<String>,
    pub range: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Conflict {
    pub package: String,
    pub chains: Vec<ConflictChain>,
    pub explanation: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SolveStats {
    pub decisions: usize,
    pub backtracks: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "status", rename_all = "lowercase")]
pub enum SolveOutcome {
    Solved {
        locked: BTreeMap<String, String>,
        decisions: Vec<String>,
        stats: SolveStats,
    },
    Unsolvable {
        conflict: Conflict,
        decisions: Vec<String>,
        stats: SolveStats,
    },
}

#[derive(Debug)]
pub enum SolveError {
    BadVersion { package: String, version: String },
    BadRange { context: String, range: String },
}

impl std::fmt::Display for SolveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SolveError::BadVersion { package, version } => {
                write!(f, "package `{package}` has invalid semver version `{version}`")
            }
            SolveError::BadRange { context, range } => {
                write!(f, "invalid semver range `{range}` ({context})")
            }
        }
    }
}

struct Indexed<'a> {
    /// name -> versions sorted by fixed preference: highest version first,
    /// registry order as tiebreak.
    versions: BTreeMap<String, Vec<(Version, &'a PackageVersion)>>,
}

fn build_index(registry: &Registry) -> Result<Indexed<'_>, SolveError> {
    let mut versions: BTreeMap<String, Vec<(Version, &PackageVersion)>> = BTreeMap::new();
    for pkg in &registry.packages {
        let entry = versions.entry(pkg.name.clone()).or_default();
        for pv in &pkg.versions {
            let v = Version::parse(&pv.version).map_err(|_| SolveError::BadVersion {
                package: pkg.name.clone(),
                version: pv.version.clone(),
            })?;
            entry.push((v, pv));
        }
    }
    // Fixed preference: highest version first; stable sort keeps registry
    // order as the deterministic tiebreak for equal versions.
    for list in versions.values_mut() {
        list.sort_by(|a, b| b.0.cmp(&a.0));
    }
    Ok(Indexed { versions })
}

/// A dependency of `pkg` is active when its platform condition matches the
/// request platform and, if optional, its extra is enabled.
pub fn dep_is_active(
    pkg_name: &str,
    dep: &crate::model::Dependency,
    platform: Option<&str>,
    extras: &[String],
) -> bool {
    if let Some(cond) = &dep.platform {
        match platform {
            Some(p) if p.eq_ignore_ascii_case(cond) => {}
            _ => return false,
        }
    }
    if let Some(extra) = &dep.optional {
        let key = format!("{pkg_name}/{extra}");
        if !extras.iter().any(|e| e == &key) {
            return false;
        }
    }
    true
}

pub struct Solver<'a> {
    index: Indexed<'a>,
    platform: Option<String>,
    extras: Vec<String>,
    decisions: Vec<String>,
    stats_decisions: usize,
    stats_backtracks: usize,
    /// Deepest conflict seen (most packages already selected when it hit).
    best_conflict: Option<(usize, Conflict)>,
}

impl<'a> Solver<'a> {
    pub fn new(
        registry: &'a Registry,
        platform: Option<String>,
        extras: Vec<String>,
    ) -> Result<Self, SolveError> {
        Ok(Solver {
            index: build_index(registry)?,
            platform,
            extras,
            decisions: Vec::new(),
            stats_decisions: 0,
            stats_backtracks: 0,
            best_conflict: None,
        })
    }

    pub fn solve(mut self, requirements: &[Requirement]) -> Result<SolveOutcome, SolveError> {
        let mut state = State::default();
        for req in requirements {
            // Validate ranges up front for a clean 400-style error.
            VersionReq::parse(&req.range).map_err(|_| SolveError::BadRange {
                context: format!("root requirement on `{}`", req.name),
                range: req.range.clone(),
            })?;
            state
                .constraints
                .entry(req.name.clone())
                .or_default()
                .push(ConstraintSource {
                    chain: vec!["<root>".to_string()],
                    range: req.range.clone(),
                });
        }
        match self.search(state) {
            Some(selected) => {
                let locked = selected
                    .into_iter()
                    .map(|(k, v)| (k, v.to_string()))
                    .collect();
                Ok(SolveOutcome::Solved {
                    locked,
                    decisions: self.decisions,
                    stats: SolveStats {
                        decisions: self.stats_decisions,
                        backtracks: self.stats_backtracks,
                    },
                })
            }
            None => {
                let conflict = self
                    .best_conflict
                    .map(|(_, c)| c)
                    .unwrap_or_else(|| Conflict {
                        package: "<unknown>".into(),
                        chains: vec![],
                        explanation: "no requirements could be processed".into(),
                    });
                Ok(SolveOutcome::Unsolvable {
                    conflict,
                    decisions: self.decisions,
                    stats: SolveStats {
                        decisions: self.stats_decisions,
                        backtracks: self.stats_backtracks,
                    },
                })
            }
        }
    }

    fn record_conflict(&mut self, depth: usize, package: &str, sources: &[ConstraintSource]) {
        let mut chains: Vec<ConflictChain> = sources
            .iter()
            .map(|s| ConflictChain {
                required_by: s.chain.clone(),
                range: s.range.clone(),
            })
            .collect();
        chains.sort();
        chains.dedup();
        let parts: Vec<String> = chains
            .iter()
            .map(|c| format!("`{}` (from {})", c.range, c.required_by.join(" → ")))
            .collect();
        let explanation = format!(
            "no version of `{package}` satisfies all constraints: {}",
            parts.join(", ")
        );
        let conflict = Conflict {
            package: package.to_string(),
            chains,
            explanation,
        };
        let better = match &self.best_conflict {
            None => true,
            Some((d, _)) => depth > *d,
        };
        if better {
            self.best_conflict = Some((depth, conflict));
        }
    }

    /// Recursive backtracking search. Returns the selected versions on success.
    fn search(&mut self, state: State) -> Option<BTreeMap<String, Version>> {
        // Pick the next undecided package: fewest candidate versions first
        // (most constrained), name as deterministic tiebreak.
        let mut best: Option<(usize, String)> = None;
        for name in state.constraints.keys() {
            if state.selected.contains_key(name) {
                continue;
            }
            let n = self.count_candidates(&state, name);
            let better = match &best {
                None => true,
                Some((bn, bname)) => (n, name.as_str()) < (*bn, bname.as_str()),
            };
            if better {
                best = Some((n, name.clone()));
            }
        }
        // No undecided package left: every constraint is satisfied.
        let Some((_, name)) = best else {
            return Some(state.selected.clone());
        };

        let candidates = self.candidates(&state, &name);
        if candidates.is_empty() {
            let sources = state.constraints.get(&name).cloned().unwrap_or_default();
            self.record_conflict(state.selected.len(), &name, &sources);
            self.decisions
                .push(format!("conflict: no candidate for `{name}`, backtracking"));
            self.stats_backtracks += 1;
            return None;
        }

        for (version, pv) in candidates {
            self.stats_decisions += 1;
            self.decisions
                .push(format!("try {name}@{version}"));
            let mut st2 = state.clone();
            st2.selected.insert(name.clone(), version.clone());
            let parent = format!("{name}@{version}");

            let mut ok = true;
            for dep in &pv.dependencies {
                if !dep_is_active(&name, dep, self.platform.as_deref(), &self.extras) {
                    continue;
                }
                let req = match VersionReq::parse(&dep.range) {
                    Ok(r) => r,
                    Err(_) => {
                        // Invalid range inside the registry: treat this
                        // candidate as unusable rather than crashing.
                        self.decisions.push(format!(
                            "skip {parent}: dependency `{}` has invalid range `{}`",
                            dep.name, dep.range
                        ));
                        ok = false;
                        break;
                    }
                };
                let mut chain = st2
                    .constraints
                    .get(&name)
                    .and_then(|s| s.first())
                    .map(|s| s.chain.clone())
                    .unwrap_or_else(|| vec!["<root>".to_string()]);
                chain.push(parent.clone());

                if let Some(sel) = st2.selected.get(&dep.name) {
                    if !req.matches(sel) {
                        // New constraint clashes with an already-selected
                        // version: record the full chain and try next candidate.
                        let mut sources =
                            st2.constraints.get(&dep.name).cloned().unwrap_or_default();
                        sources.push(ConstraintSource {
                            chain,
                            range: dep.range.clone(),
                        });
                        self.record_conflict(st2.selected.len(), &dep.name, &sources);
                        self.decisions.push(format!(
                            "conflict: {parent} requires `{} {}` but `{}@{}` is selected, backtracking",
                            dep.name, dep.range, dep.name, sel
                        ));
                        self.stats_backtracks += 1;
                        ok = false;
                        break;
                    }
                }
                st2.constraints
                    .entry(dep.name.clone())
                    .or_default()
                    .push(ConstraintSource {
                        chain,
                        range: dep.range.clone(),
                    });
            }
            if !ok {
                continue;
            }
            if let Some(solution) = self.search(st2) {
                return Some(solution);
            }
        }
        None
    }

    fn count_candidates(&self, state: &State, name: &str) -> usize {
        self.candidates(state, name).len()
    }

    /// Versions of `name` satisfying every accumulated constraint, in fixed
    /// preference order (highest first).
    fn candidates(&self, state: &State, name: &str) -> Vec<(Version, &'a PackageVersion)> {
        let Some(versions) = self.index.versions.get(name) else {
            return vec![];
        };
        let Some(sources) = state.constraints.get(name) else {
            return vec![];
        };
        let reqs: Vec<VersionReq> = sources
            .iter()
            .filter_map(|s| VersionReq::parse(&s.range).ok())
            .collect();
        versions
            .iter()
            .filter(|(v, _)| reqs.iter().all(|r| r.matches(v)))
            .cloned()
            .collect()
    }
}

/// Verify a lockfile against a registry: every locked version must exist,
/// every root requirement and every active dependency of every locked
/// package must be satisfied by the locked set.
pub fn verify_lock(
    registry: &Registry,
    requirements: &[Requirement],
    platform: Option<&str>,
    extras: &[String],
    locked: &BTreeMap<String, String>,
) -> Result<Vec<String>, SolveError> {
    let index = build_index(registry)?;
    let mut errors = Vec::new();

    let mut locked_versions: BTreeMap<String, Version> = BTreeMap::new();
    for (name, vs) in locked {
        let v = Version::parse(vs).map_err(|_| SolveError::BadVersion {
            package: name.clone(),
            version: vs.clone(),
        })?;
        match index.versions.get(name) {
            Some(list) if list.iter().any(|(ev, _)| ev == &v) => {}
            _ => errors.push(format!("locked `{name}@{vs}` does not exist in registry")),
        }
        locked_versions.insert(name.clone(), v);
    }

    for req in requirements {
        let rq = VersionReq::parse(&req.range).map_err(|_| SolveError::BadRange {
            context: format!("root requirement on `{}`", req.name),
            range: req.range.clone(),
        })?;
        match locked_versions.get(&req.name) {
            Some(v) if rq.matches(v) => {}
            Some(v) => errors.push(format!(
                "root requirement `{} {}` not satisfied by locked `{v}`",
                req.name, req.range
            )),
            None => errors.push(format!("root requirement `{}` missing from lock", req.name)),
        }
    }

    for (name, v) in &locked_versions {
        let Some(list) = index.versions.get(name) else {
            continue; // already reported above
        };
        let Some((_, pv)) = list.iter().find(|(ev, _)| ev == v) else {
            continue;
        };
        for dep in &pv.dependencies {
            if !dep_is_active(name, dep, platform, extras) {
                continue;
            }
            let rq = match VersionReq::parse(&dep.range) {
                Ok(r) => r,
                Err(_) => {
                    errors.push(format!(
                        "`{name}@{v}` dependency `{}` has invalid range `{}`",
                        dep.name, dep.range
                    ));
                    continue;
                }
            };
            match locked_versions.get(&dep.name) {
                Some(dv) if rq.matches(dv) => {}
                Some(dv) => errors.push(format!(
                    "`{name}@{v}` requires `{} {}` but lock has `{dv}`",
                    dep.name, dep.range
                )),
                None => errors.push(format!(
                    "`{name}@{v}` requires `{} {}` but it is missing from the lock",
                    dep.name, dep.range
                )),
            }
        }
    }
    Ok(errors)
}
