//! Brute-force oracle for small instances.
//!
//! Enumerates every combination of one-version-per-package over the package
//! universe touched by the problem, and checks feasibility directly. Used to
//! cross-check the backtracking solver: on small fixtures the two must agree.
//!
//! Feasibility of an assignment:
//! 1. root requirements are satisfied
//! 2. for every selected package, every *active* (platform + optional feature)
//!    dependency points to a selected version satisfying its range
//! 3. packages not selected carry no active incoming edge

use std::collections::{BTreeMap, BTreeSet};

use semver::Version;

use crate::model::{RootRequirement, SolveRequest};
use crate::solver::edge_active;

pub const ORACLE_NODE_LIMIT: u64 = 200_000;

pub struct OracleResult {
    pub satisfiable: bool,
    pub combinations_explored: u64,
    pub truncated: bool,
}

/// Relevant package universe: every package named by a root requirement plus
/// every package reachable through any registry version (platform/optional
/// filters applied per the request).
fn universe(req: &SolveRequest) -> BTreeSet<String> {
    let mut seen = BTreeSet::new();
    let mut stack: Vec<String> = req.requirements.iter().map(|r| r.name.clone()).collect();
    while let Some(name) = stack.pop() {
        if !seen.insert(name.clone()) {
            continue;
        }
        if let Some(versions) = req.registry.packages.get(&name) {
            for pv in versions {
                for dep in &pv.dependencies {
                    if edge_active(req, &name, pv, dep) {
                        stack.push(dep.name.clone());
                    }
                }
            }
        }
    }
    seen
}

pub fn brute_force(req: &SolveRequest) -> OracleResult {
    let universe = universe(req);
    let mut domains: Vec<(String, Vec<Version>)> = Vec::new();
    for name in &universe {
        let versions: Vec<Version> = req
            .registry
            .packages
            .get(name)
            .map(|vs| vs.iter().filter(|pv| !pv.yanked).map(|pv| pv.version.clone()).collect())
            .unwrap_or_default();
        // A package with zero published versions has only the "absent" slot;
        // enumeration then finds no feasible assignment for forced packages.
        domains.push((name.clone(), versions));
    }

    // Each package may be "absent" (index versions.len()) unless a root
    // requirement forces it present.
    let forced: BTreeSet<String> = req.requirements.iter().map(|r| r.name.clone()).collect();

    let mut combo: Vec<usize> = vec![0; domains.len()];
    let mut explored: u64 = 0;

    loop {
        // Build the assignment for this combination.
        let mut assignment: BTreeMap<String, Version> = BTreeMap::new();
        let mut valid = true;
        for (i, (name, versions)) in domains.iter().enumerate() {
            let idx = combo[i];
            if idx == versions.len() {
                if forced.contains(name) {
                    valid = false;
                    break;
                }
            } else {
                assignment.insert(name.clone(), versions[idx].clone());
            }
        }

        if valid {
            explored += 1;
            if feasible(req, &assignment) {
                return OracleResult {
                    satisfiable: true,
                    combinations_explored: explored,
                    truncated: false,
                };
            }
        } else {
            explored += 1;
        }

        if explored >= ORACLE_NODE_LIMIT {
            return OracleResult {
                satisfiable: false,
                combinations_explored: explored,
                truncated: true,
            };
        }

        // Mixed-radix increment.
        let mut k = 0;
        loop {
            if k == domains.len() {
                return OracleResult {
                    satisfiable: false,
                    combinations_explored: explored,
                    truncated: false,
                };
            }
            combo[k] += 1;
            let radix = domains[k].1.len() + 1;
            if combo[k] < radix {
                break;
            }
            combo[k] = 0;
            k += 1;
        }
    }
}

fn feasible(req: &SolveRequest, a: &BTreeMap<String, Version>) -> bool {
    let allow_all = req.allow_prerelease;

    let prerelease_ok = |v: &Version, range: &crate::semver_range::Req| {
        let allow = allow_all || v.pre == semver::Prerelease::EMPTY || range.allows_prerelease_of(v);
        range.matches(v, allow)
    };

    for RootRequirement { name, req: range } in &req.requirements {
        match a.get(name) {
            Some(v) if prerelease_ok(v, range) => {}
            _ => return false,
        }
    }
    // Pin preferences.
    for (name, pref) in &req.preferences {
        if let crate::model::Preference::Pin { version } = pref {
            if a.get(name) != Some(version) {
                return false;
            }
        }
    }

    // Incoming-edge tracking for absent packages.
    let mut incoming: BTreeMap<String, bool> = BTreeMap::new();
    for (name, version) in a {
        let Some(pv) = req
            .registry
            .packages
            .get(name)
            .and_then(|vs| vs.iter().find(|pv| &pv.version == version))
        else {
            return false;
        };
        for dep in &pv.dependencies {
            if !edge_active(req, name, pv, dep) {
                continue;
            }
            incoming.entry(dep.name.clone()).or_insert(false);
            let Some(target) = a.get(&dep.name) else {
                return false;
            };
            if !prerelease_ok(target, &dep.req) {
                return false;
            }
            incoming.insert(dep.name.clone(), true);
        }
    }
    let _ = incoming;
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures;

    #[test]
    fn diamond_satisfiable() {
        assert!(brute_force(&fixtures::diamond()).satisfiable);
    }

    #[test]
    fn mutex_unsatisfiable() {
        assert!(!brute_force(&fixtures::mutex()).satisfiable);
    }

    #[test]
    fn cycle_with_unconstrained_dep_satisfiable() {
        assert!(brute_force(&fixtures::cycle_ok()).satisfiable);
    }
}
