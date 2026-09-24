//! Small-scale exhaustive check: for many randomly generated tiny registries,
//! compare the solver's solved/unsolvable verdict against brute-force
//! enumeration of every possible version assignment. Also verify that every
//! lock the solver produces passes lockfile replay (`verify_lock`).

use depsolver::model::*;
use depsolver::solver::{dep_is_active, verify_lock, SolveOutcome, Solver};
use semver::{Version, VersionReq};
use std::collections::BTreeMap;

/// Deterministic xorshift RNG so the test is reproducible without extra deps.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn chance(&mut self, pct: u64) -> bool {
        self.below(100) < pct
    }
}

const VERSION_POOL: [&str; 5] = ["0.1.0", "0.2.0", "1.0.0", "1.1.0", "2.0.0"];
const RANGE_POOL: [&str; 8] = [
    "*",
    ">=0.2.0",
    ">=1.0.0",
    "<1.0.0",
    ">=1.0.0, <2.0.0",
    ">=2.0.0",
    "<0.2.0",
    "=1.0.0",
];
const PLATFORMS: [&str; 2] = ["linux", "windows"];

fn gen_registry(rng: &mut Rng, n_packages: usize) -> Registry {
    let mut packages = Vec::new();
    for i in 0..n_packages {
        let name = format!("p{i}");
        // 1..=3 distinct versions from the pool.
        let n_versions = 1 + rng.below(3) as usize;
        let mut idxs: Vec<usize> = (0..VERSION_POOL.len()).collect();
        // Fisher-Yates shuffle, truncated.
        for j in 0..idxs.len() {
            let k = j + rng.below((idxs.len() - j) as u64) as usize;
            idxs.swap(j, k);
        }
        idxs.truncate(n_versions);
        idxs.sort_unstable();

        let mut versions = Vec::new();
        for &vi in &idxs {
            let mut dependencies = Vec::new();
            for j in 0..n_packages {
                if j == i || !rng.chance(40) {
                    continue;
                }
                let range = RANGE_POOL[rng.below(RANGE_POOL.len() as u64) as usize].to_string();
                let optional = if rng.chance(15) {
                    Some("x".to_string())
                } else {
                    None
                };
                let platform = if rng.chance(15) {
                    Some(PLATFORMS[rng.below(2) as usize].to_string())
                } else {
                    None
                };
                dependencies.push(Dependency {
                    name: format!("p{j}"),
                    range,
                    optional,
                    platform,
                });
            }
            versions.push(PackageVersion {
                version: VERSION_POOL[vi].to_string(),
                dependencies,
            });
        }
        packages.push(Package { name, versions });
    }
    Registry { packages }
}

/// Brute-force satisfiability: does any assignment (each package absent or
/// pinned to one of its versions) satisfy all root requirements and close
/// under all active dependencies?
fn brute_force_solvable(
    reg: &Registry,
    reqs: &[Requirement],
    platform: Option<&str>,
    extras: &[String],
) -> bool {
    let parsed: Vec<(String, Vec<(Version, &PackageVersion)>)> = reg
        .packages
        .iter()
        .map(|p| {
            (
                p.name.clone(),
                p.versions
                    .iter()
                    .map(|pv| (Version::parse(&pv.version).unwrap(), pv))
                    .collect(),
            )
        })
        .collect();
    let req_parsed: Vec<(&str, VersionReq)> = reqs
        .iter()
        .map(|r| (r.name.as_str(), VersionReq::parse(&r.range).unwrap()))
        .collect();

    // choices[i] = None (absent) or Some(version index).
    let n = parsed.len();
    let mut choices: Vec<Option<usize>> = vec![None; n];

    fn check(
        parsed: &[(String, Vec<(Version, &PackageVersion)>)],
        req_parsed: &[(&str, VersionReq)],
        choices: &[Option<usize>],
        platform: Option<&str>,
        extras: &[String],
    ) -> bool {
        let selected: BTreeMap<&str, (&Version, &PackageVersion)> = parsed
            .iter()
            .zip(choices)
            .filter_map(|((name, versions), c)| {
                c.map(|i| {
                    let (v, pv) = &versions[i];
                    (name.as_str(), (v, *pv))
                })
            })
            .collect();
        // Root requirements must be satisfied.
        for (name, rq) in req_parsed {
            match selected.get(name) {
                Some((v, _)) if rq.matches(v) => {}
                _ => return false,
            }
        }
        // Closure: every active dependency of every selected package must be
        // selected and matching.
        for (name, (_, pv)) in &selected {
            for dep in &pv.dependencies {
                if !dep_is_active(name, dep, platform, extras) {
                    continue;
                }
                let rq = match VersionReq::parse(&dep.range) {
                    Ok(r) => r,
                    Err(_) => return false, // invalid range: version unusable
                };
                match selected.get(dep.name.as_str()) {
                    Some((v, _)) if rq.matches(v) => {}
                    _ => return false,
                }
            }
        }
        true
    }

    fn enumerate(
        i: usize,
        parsed: &[(String, Vec<(Version, &PackageVersion)>)],
        req_parsed: &[(&str, VersionReq)],
        choices: &mut [Option<usize>],
        platform: Option<&str>,
        extras: &[String],
    ) -> bool {
        if i == parsed.len() {
            return check(parsed, req_parsed, choices, platform, extras);
        }
        // Option 1: package absent.
        choices[i] = None;
        if enumerate(i + 1, parsed, req_parsed, choices, platform, extras) {
            return true;
        }
        // Option 2: pinned to each version.
        for vi in 0..parsed[i].1.len() {
            choices[i] = Some(vi);
            if enumerate(i + 1, parsed, req_parsed, choices, platform, extras) {
                return true;
            }
        }
        choices[i] = None;
        false
    }

    let _ = n;
    enumerate(0, &parsed, &req_parsed, &mut choices, platform, extras)
}

#[test]
fn exhaustive_small_registries_match_brute_force() {
    let mut rng = Rng(0x5DEECE66D);
    let cases = 400;
    let mut solved_count = 0usize;
    let mut unsolvable_count = 0usize;

    for case in 0..cases {
        let n_packages = 2 + rng.below(2) as usize; // 2 or 3 packages
        let reg = gen_registry(&mut rng, n_packages);

        // 1-2 root requirements over existing packages.
        let n_reqs = 1 + rng.below(2) as usize;
        let reqs: Vec<Requirement> = (0..n_reqs)
            .map(|_| Requirement {
                name: format!("p{}", rng.below(n_packages as u64)),
                range: RANGE_POOL[rng.below(RANGE_POOL.len() as u64) as usize].to_string(),
            })
            .collect();

        let platform = match rng.below(3) {
            0 => None,
            1 => Some("linux".to_string()),
            _ => Some("windows".to_string()),
        };
        let extras: Vec<String> = (0..n_packages)
            .filter(|_| rng.chance(30))
            .map(|i| format!("p{i}/x"))
            .collect();

        let outcome = Solver::new(&reg, platform.clone(), extras.clone())
            .unwrap()
            .solve(&reqs)
            .unwrap();
        let brute = brute_force_solvable(&reg, &reqs, platform.as_deref(), &extras);

        match &outcome {
            SolveOutcome::Solved { locked, .. } => {
                solved_count += 1;
                assert!(
                    brute,
                    "case {case}: solver found a solution but brute force found none\n\
                     registry: {}\nreqs: {reqs:?} platform: {platform:?} extras: {extras:?}",
                    serde_json::to_string_pretty(&reg).unwrap()
                );
                // The produced lock must replay cleanly against the registry.
                let errors =
                    verify_lock(&reg, &reqs, platform.as_deref(), &extras, locked).unwrap();
                assert!(
                    errors.is_empty(),
                    "case {case}: solver lock failed replay verification: {errors:?}"
                );
            }
            SolveOutcome::Unsolvable { conflict, .. } => {
                unsolvable_count += 1;
                assert!(
                    !brute,
                    "case {case}: solver said unsolvable but brute force found a solution\n\
                     conflict: {}\nregistry: {}\nreqs: {reqs:?} platform: {platform:?} extras: {extras:?}",
                    conflict.explanation,
                    serde_json::to_string_pretty(&reg).unwrap()
                );
            }
        }
    }

    // Sanity: the generator must actually produce both outcomes, otherwise
    // the comparison is vacuous.
    eprintln!("exhaustive: {cases} cases -> {solved_count} solved, {unsolvable_count} unsolvable");
    assert!(solved_count > 0, "no solvable cases generated");
    assert!(unsolvable_count > 0, "no unsolvable cases generated");
}
