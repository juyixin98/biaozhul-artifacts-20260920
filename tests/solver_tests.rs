//! Scenario tests: diamond dependencies, mutually exclusive ranges,
//! backtracking, cycles, optional deps, platform conditions, lockfile replay.

use depsolver::model::*;
use depsolver::solver::{verify_lock, SolveOutcome, Solver};
use serde_json::json;

fn registry(v: serde_json::Value) -> Registry {
    serde_json::from_value(json!({ "packages": v })).unwrap()
}

fn req(name: &str, range: &str) -> Requirement {
    Requirement {
        name: name.into(),
        range: range.into(),
    }
}

fn solve(reg: &Registry, reqs: &[Requirement]) -> SolveOutcome {
    Solver::new(reg, None, vec![])
        .unwrap()
        .solve(reqs)
        .unwrap()
}

fn solved(outcome: &SolveOutcome) -> &std::collections::BTreeMap<String, String> {
    match outcome {
        SolveOutcome::Solved { locked, .. } => locked,
        other => panic!("expected solved, got {}", serde_json::to_string_pretty(other).unwrap()),
    }
}

fn unsolvable(outcome: &SolveOutcome) -> &depsolver::solver::Conflict {
    match outcome {
        SolveOutcome::Unsolvable { conflict, .. } => conflict,
        other => panic!(
            "expected unsolvable, got {}",
            serde_json::to_string_pretty(other).unwrap()
        ),
    }
}

/// Diamond: root -> a, root -> b, a -> c ^1, b -> c >=1.0.0,<2.0.0.
/// Both constraints on c are compatible; the highest 1.x must be chosen.
#[test]
fn diamond_dependency() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": "^1.0.0" } ] } ] },
        { "name": "b", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": ">=1.0.0, <2.0.0" } ] } ] },
        { "name": "c", "versions": [
            { "version": "1.0.0" }, { "version": "1.5.0" }, { "version": "2.0.0" } ] }
    ]));
    let out = solve(&reg, &[req("a", "^1.0.0"), req("b", "^1.0.0")]);
    let locked = solved(&out);
    assert_eq!(locked["a"], "1.0.0");
    assert_eq!(locked["b"], "1.0.0");
    assert_eq!(locked["c"], "1.5.0", "highest version satisfying both constraints");
}

/// Mutually exclusive ranges: a needs c >= 2, b needs c < 2 -> unsolvable,
/// and the conflict chain must name both paths.
#[test]
fn mutually_exclusive_ranges_explain_conflict() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": ">=2.0.0" } ] } ] },
        { "name": "b", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": "<2.0.0" } ] } ] },
        { "name": "c", "versions": [
            { "version": "1.0.0" }, { "version": "2.0.0" } ] }
    ]));
    let out = solve(&reg, &[req("a", "^1.0.0"), req("b", "^1.0.0")]);
    let conflict = unsolvable(&out);
    assert_eq!(conflict.package, "c");
    let text = conflict.explanation.clone();
    assert!(text.contains(">=2.0.0"), "explanation must mention a's constraint: {text}");
    assert!(text.contains("<2.0.0"), "explanation must mention b's constraint: {text}");
    // Both provenance chains present: <root> -> a@1.0.0 and <root> -> b@1.0.0
    let chains = serde_json::to_string(&conflict.chains).unwrap();
    assert!(chains.contains("a@1.0.0"), "chain via a: {chains}");
    assert!(chains.contains("b@1.0.0"), "chain via b: {chains}");
}

/// Greedy highest-first fails (a@2.0.0 needs c@^2 which does not exist);
/// the solver must backtrack and pick a@1.0.0 instead of declaring unsolvable.
#[test]
fn backtracks_instead_of_greedy_failure() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "2.0.0", "dependencies": [
                { "name": "c", "range": "^2.0.0" } ] },
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": "^1.0.0" } ] } ] },
        { "name": "c", "versions": [ { "version": "1.0.0" } ] }
    ]));
    let out = solve(&reg, &[req("a", ">=1.0.0")]);
    match &out {
        SolveOutcome::Solved { locked, stats, .. } => {
            assert_eq!(locked["a"], "1.0.0");
            assert_eq!(locked["c"], "1.0.0");
            assert!(stats.backtracks >= 1, "must have backtracked: {stats:?}");
        }
        other => panic!("expected solved via backtracking, got {other:?}"),
    }
}

/// Compatible cycle: a <-> b. Must terminate and solve.
#[test]
fn cyclic_dependency_compatible() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "b", "range": "^1.0.0" } ] } ] },
        { "name": "b", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "a", "range": "^1.0.0" } ] } ] }
    ]));
    let out = solve(&reg, &[req("a", "^1.0.0")]);
    let locked = solved(&out);
    assert_eq!(locked["a"], "1.0.0");
    assert_eq!(locked["b"], "1.0.0");
}

/// Incompatible cycle: a@1 needs b^1, b@1 needs a^2, a@2 needs b^2 (missing).
/// Every path fails; the solver must report unsolvable with a conflict chain.
#[test]
fn cyclic_dependency_unsolvable() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "b", "range": "^1.0.0" } ] },
            { "version": "2.0.0", "dependencies": [
                { "name": "b", "range": "^2.0.0" } ] } ] },
        { "name": "b", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "a", "range": "^2.0.0" } ] } ] }
    ]));
    let out = solve(&reg, &[req("a", ">=1.0.0")]);
    let conflict = unsolvable(&out);
    assert!(
        conflict.package == "a" || conflict.package == "b",
        "conflict should be on a or b, got {}",
        conflict.package
    );
    assert!(!conflict.chains.is_empty());
}

/// Optional dependency only activates when its extra is enabled.
#[test]
fn optional_dependency_via_extras() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "tls", "range": "^1.0.0", "optional": "secure" } ] } ] },
        { "name": "tls", "versions": [ { "version": "1.2.0" } ] }
    ]));
    // Without the extra: tls is not pulled in.
    let out = Solver::new(&reg, None, vec![])
        .unwrap()
        .solve(&[req("a", "^1.0.0")])
        .unwrap();
    let locked = solved(&out);
    assert!(!locked.contains_key("tls"));

    // With the extra enabled: tls must be locked.
    let out = Solver::new(&reg, None, vec!["a/secure".to_string()])
        .unwrap()
        .solve(&[req("a", "^1.0.0")])
        .unwrap();
    let locked = solved(&out);
    assert_eq!(locked["tls"], "1.2.0");
}

/// Platform-conditional dependency only activates on the matching platform.
#[test]
fn platform_conditional_dependency() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "winapi", "range": "^1.0.0", "platform": "windows" },
                { "name": "libc", "range": "^1.0.0", "platform": "linux" } ] } ] },
        { "name": "winapi", "versions": [ { "version": "1.0.0" } ] },
        { "name": "libc", "versions": [ { "version": "1.0.0" } ] }
    ]));
    let out = Solver::new(&reg, Some("linux".into()), vec![])
        .unwrap()
        .solve(&[req("a", "^1.0.0")])
        .unwrap();
    let locked = solved(&out);
    assert_eq!(locked["libc"], "1.0.0");
    assert!(!locked.contains_key("winapi"));

    let out = Solver::new(&reg, Some("windows".into()), vec![])
        .unwrap()
        .solve(&[req("a", "^1.0.0")])
        .unwrap();
    let locked = solved(&out);
    assert_eq!(locked["winapi"], "1.0.0");
    assert!(!locked.contains_key("libc"));
}

/// Lockfile replay: verify accepts the produced lock, rejects tampering,
/// and re-resolving is deterministic (identical lock).
#[test]
fn lockfile_replay_is_consistent() {
    let reg = registry(json!([
        { "name": "a", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": "^1.0.0" } ] } ] },
        { "name": "b", "versions": [
            { "version": "1.0.0", "dependencies": [
                { "name": "c", "range": ">=1.0.0, <2.0.0" } ] } ] },
        { "name": "c", "versions": [
            { "version": "1.0.0" }, { "version": "1.5.0" }, { "version": "2.0.0" } ] }
    ]));
    let reqs = vec![req("a", "^1.0.0"), req("b", "^1.0.0")];

    let out1 = solve(&reg, &reqs);
    let locked1 = solved(&out1).clone();

    // Determinism: same input -> identical lock.
    let out2 = solve(&reg, &reqs);
    assert_eq!(&locked1, solved(&out2));

    // Replay: produced lock verifies cleanly.
    let errors = verify_lock(&reg, &reqs, None, &[], &locked1).unwrap();
    assert!(errors.is_empty(), "replay of produced lock failed: {errors:?}");

    // Tampered lock (downgrade c below a's constraint... use a version that
    // still exists but violates ^1.0.0 from a? c@2.0.0 violates b's <2.0.0).
    let mut tampered = locked1.clone();
    tampered.insert("c".into(), "2.0.0".into());
    let errors = verify_lock(&reg, &reqs, None, &[], &tampered).unwrap();
    assert!(!errors.is_empty(), "tampered lock must not verify");
    assert!(errors.iter().any(|e| e.contains("c")), "{errors:?}");

    // Missing package in lock.
    let mut missing = locked1.clone();
    missing.remove("c");
    let errors = verify_lock(&reg, &reqs, None, &[], &missing).unwrap();
    assert!(!errors.is_empty());

    // Nonexistent locked version.
    let mut ghost = locked1.clone();
    ghost.insert("c".into(), "9.9.9".into());
    let errors = verify_lock(&reg, &reqs, None, &[], &ghost).unwrap();
    assert!(errors.iter().any(|e| e.contains("does not exist")), "{errors:?}");
}

/// Unknown root package is unsolvable with a readable explanation.
#[test]
fn unknown_package_reports_conflict() {
    let reg = registry(json!([
        { "name": "a", "versions": [ { "version": "1.0.0" } ] }
    ]));
    let out = solve(&reg, &[req("ghost", "^1.0.0")]);
    let conflict = unsolvable(&out);
    assert_eq!(conflict.package, "ghost");
    assert!(conflict.explanation.contains("ghost"));
}
