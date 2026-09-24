//! Lockfile construction, input fingerprinting and deterministic replay.

use std::collections::BTreeMap;

use semver::Version;
use serde::Serialize;

use crate::model::{
    ChangedPackage, LockedPackage, Lockfile, Mismatch, ReplayRequest, ReplayResponse,
    SolveRequest, SolveResponse,
};
use crate::solver::{run as run_solve, Solution, SolveOutcome};

/// 128-bit FNV-1a over a canonical JSON serialization of the solve inputs.
/// Non-cryptographic; its only job is to detect tampering/drift between
/// solve-time and replay-time inputs.
pub fn fingerprint(req: &SolveRequest) -> String {
    let canonical = serde_json::to_string(&FingerprintInput::from(req))
        .expect("canonical serialization is infallible");
    let mut hash: u128 = 0x6c62272e07bb0142_62b821756295c58d;
    for byte in canonical.as_bytes() {
        hash ^= *byte as u128;
        hash = hash.wrapping_mul(0x0000000001000000_000000000000013b);
    }
    format!("{hash:032x}")
}

/// Everything that can change a solution. Response-only flags (`verify`) are
/// excluded so toggling verification does not invalidate a lock.
#[derive(Serialize)]
struct FingerprintInput<'a> {
    registry: &'a crate::model::Registry,
    requirements: &'a [crate::model::RootRequirement],
    features: BTreeMap<&'a String, &'a Vec<String>>,
    platform: &'a Option<crate::platform::Platform>,
    preferences: &'a BTreeMap<String, crate::model::Preference>,
    default_preference: &'a crate::model::DefaultPref,
    allow_prerelease: bool,
}

impl<'a> From<&'a SolveRequest> for FingerprintInput<'a> {
    fn from(req: &'a SolveRequest) -> Self {
        FingerprintInput {
            registry: &req.registry,
            requirements: &req.requirements,
            features: req.features.iter().collect(),
            platform: &req.platform,
            preferences: &req.preferences,
            default_preference: &req.default_preference,
            allow_prerelease: req.allow_prerelease,
        }
    }
}

pub fn build_lockfile(req: &SolveRequest, sol: &Solution) -> Lockfile {
    let mut packages = Vec::new();
    for (name, version) in &sol.assignment {
        let mut dependencies = BTreeMap::new();
        if let Some(edges) = sol.edges.get(name) {
            for e in edges {
                dependencies.insert(e.to.clone(), e.req.to_string());
            }
        }
        let mut via = Vec::new();
        for (from, edges) in &sol.edges {
            for e in edges {
                if &e.to == name {
                    let v = &sol.assignment[from];
                    via.push(format!("{from}@{v}"));
                }
            }
        }
        if via.is_empty() && req.requirements.iter().any(|r| &r.name == name) {
            via.push("root".into());
        }
        via.sort();
        via.dedup();
        packages.push(LockedPackage {
            name: name.clone(),
            version: version.clone(),
            dependencies,
            via,
        });
    }
    packages.sort_by(|a, b| a.name.cmp(&b.name));
    Lockfile { version: 1, packages, input_fingerprint: fingerprint(req) }
}

/// Re-solve and compare against the stored lock.
pub fn replay(input: ReplayRequest) -> ReplayResponse {
    let ReplayRequest { request, lockfile } = input;
    let fp_changed = fingerprint(&request) != lockfile.input_fingerprint;
    let SolveOutcome { result, stats, notes: _ } = run_solve(&request);

    match result {
        Err(unsat) => ReplayResponse {
            status: "mismatch",
            mismatch: Some(Mismatch {
                fingerprint_changed: fp_changed,
                missing: vec![],
                extra: vec![],
                changed: vec![],
                unsat: Some(unsat),
            }),
            stats: Some(stats),
        },
        Ok(sol) => {
            let fresh = build_lockfile(&request, &sol);
            let locked: BTreeMap<&str, &Version> =
                lockfile.packages.iter().map(|p| (p.name.as_str(), &p.version)).collect();
            let resolved: BTreeMap<&str, &Version> =
                fresh.packages.iter().map(|p| (p.name.as_str(), &p.version)).collect();

            let mut missing = Vec::new();
            let mut extra = Vec::new();
            let mut changed = Vec::new();
            for (name, v) in &resolved {
                match locked.get(name) {
                    None => missing.push((*name).to_string()),
                    Some(lv) if lv != v => changed.push(ChangedPackage {
                        name: (*name).to_string(),
                        locked: (**lv).clone(),
                        resolved: (**v).clone(),
                    }),
                    Some(_) => {}
                }
            }
            for name in locked.keys() {
                if !resolved.contains_key(name) {
                    extra.push((*name).to_string());
                }
            }
            missing.sort();
            extra.sort();
            changed.sort_by(|a, b| a.name.cmp(&b.name));

            let same = !fp_changed && missing.is_empty() && extra.is_empty() && changed.is_empty();
            if same {
                ReplayResponse { status: "matches", mismatch: None, stats: Some(stats) }
            } else {
                ReplayResponse {
                    status: "mismatch",
                    mismatch: Some(Mismatch {
                        fingerprint_changed: fp_changed,
                        missing,
                        extra,
                        changed,
                        unsat: None,
                    }),
                    stats: Some(stats),
                }
            }
        }
    }
}

/// Run a solve, optionally cross-check with the brute-force oracle, and wrap
/// the result in the HTTP response body.
pub fn solve_response(request: &SolveRequest) -> SolveResponse {
    let SolveOutcome { result, mut stats, notes } = run_solve(request);

    if request.verify {
        let oracle = crate::oracle::brute_force(request);
        let agrees = oracle.truncated || oracle.satisfiable == result.is_ok();
        stats.oracle = Some(crate::model::OracleVerdict {
            satisfiable: oracle.satisfiable,
            combinations_explored: oracle.combinations_explored,
            agrees,
            truncated: oracle.truncated,
        });
    }

    match result {
        Ok(sol) => {
            let lockfile = build_lockfile(request, &sol);
            let cycles = if sol.cycles.is_empty() { None } else { Some(sol.cycles.clone()) };
            SolveResponse {
                status: "satisfiable",
                lockfile: Some(lockfile),
                cycles,
                stats: Some(stats),
                conflict: None,
                notes,
            }
        }
        Err(conflict) => SolveResponse {
            status: "unsatisfiable",
            lockfile: None,
            cycles: None,
            stats: Some(stats),
            conflict: Some(conflict),
            notes,
        },
    }
}
