//! End-to-end acceptance tests for the solver service.
//!
//! These are binary-level integration tests: because the crate is a binary
//! crate, they reuse the unit tests in `src/` for internals and go through
//! the public HTTP surface (plus the bundled fixture inputs) for behavior.

// The crate is a binary crate with no lib target, so integration tests cannot
// `use depres::*`. Instead we shell out to the built server over HTTP after
// launching it, which also exercises real Axum routing and JSON (de)serialization.
//
// The scenario payloads themselves are kept in `examples/` as curl-ready JSON.

mod harness;

use harness::Server;
use serde_json::{json, Value};

#[test]
fn health_ok() {
    let s = Server::start();
    let v: Value = s.get_json("/health");
    assert_eq!(v["status"], "ok");
}

#[test]
fn diamond_resolves_with_backtracking() {
    let s = Server::start();
    let req = s.fixture("diamond");
    let out = s.solve(&req);

    assert_eq!(out["status"], "satisfiable");
    let pkgs = &out["lockfile"]["packages"];
    let by = |n: &str| {
        pkgs.as_array()
            .unwrap()
            .iter()
            .find(|p| p["name"] == n)
            .unwrap()
    };
    // The only jointly-feasible combination: a 1.0.0 + x 1.4.5.
    assert_eq!(by("a")["version"], "1.0.0");
    assert_eq!(by("b")["version"], "1.0.0");
    assert_eq!(by("x")["version"], "1.4.5");
    // Backtracking genuinely happened (a 1.1.0 was tried and rolled back).
    assert!(out["stats"]["backtracks"].as_u64().unwrap() >= 1);
    // Oracle agrees.
    assert_eq!(out["stats"]["oracle"]["satisfiable"], true);
    assert_eq!(out["stats"]["oracle"]["agrees"], true);
}

#[test]
fn mutex_ranges_are_unsatisfiable_with_conflict_chain() {
    let s = Server::start();
    let req = s.fixture("mutex");
    let out = s.solve(&req);

    assert_eq!(out["status"], "unsatisfiable");
    assert!(out["lockfile"].is_null());

    // The decision frames nest a -> b -> x; the actual mutex is the leaf
    // conflict on `x`. Walk the nested chain to it.
    fn deepest(c: &Value) -> &Value {
        let mut cur = c;
        while let Some(n) = cur["nested"].as_array().and_then(|a| a.first()) {
            cur = n;
        }
        cur
    }
    let root = &out["conflict"];
    let c = deepest(root);
    assert_eq!(c["package"], "x");

    // Two conflicting constraints must both appear.
    let constraints: Vec<String> = c["trace"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["constraint"].as_str().unwrap().to_string())
        .collect();
    assert!(constraints.iter().any(|s| s.contains("^1.0")), "constraints={constraints:?}");
    assert!(constraints.iter().any(|s| s.contains("^2.0")), "constraints={constraints:?}");
    // Origins identify the two sides of the mutex.
    let from: Vec<String> = c["trace"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["from"].as_str().unwrap().to_string())
        .collect();
    assert!(from.iter().any(|f| f == "a"));
    assert!(from.iter().any(|f| f == "b"));
    // The full decision chain is preserved at the top.
    assert_eq!(root["package"], "a");
    assert_eq!(root["nested"][0]["package"], "b");
    // Oracle agrees on unsatisfiability.
    assert_eq!(out["stats"]["oracle"]["satisfiable"], false);
    assert_eq!(out["stats"]["oracle"]["agrees"], true);
}

#[test]
fn satisfiable_cycle_is_locked_and_reported() {
    let s = Server::start();
    let req = s.fixture("cycle_ok");
    let out = s.solve(&req);

    assert_eq!(out["status"], "satisfiable");
    let cycles = out["cycles"].as_array().unwrap();
    assert_eq!(cycles.len(), 1);
    let names: Vec<String> = cycles[0].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
    assert_eq!(names, vec!["ping", "pong"]);
    assert_eq!(out["stats"]["oracle"]["satisfiable"], true);

    // Replay the same lockfile against the same request: identical.
    let replay = s.replay(json!({
        "request": req,
        "lockfile": out["lockfile"],
    }));
    assert_eq!(replay["status"], "matches");
}

#[test]
fn circular_version_mutex_is_unsatisfiable() {
    let s = Server::start();
    let req = s.fixture("cycle_unsat");
    let out = s.solve(&req);
    assert_eq!(out["status"], "unsatisfiable");
    assert_eq!(out["stats"]["oracle"]["satisfiable"], false);
}

#[test]
fn optional_and_platform_conditional_dependencies() {
    let s = Server::start();
    let mut req = s.fixture("optional");

    // Linux + feature "fast" -> everything selected.
    let out = s.solve(&req);
    assert_eq!(out["status"], "satisfiable");
    let names: Vec<String> = out["lockfile"]["packages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["name"].as_str().unwrap().to_string())
        .collect();
    for n in ["app", "core", "cache", "io_uring"] {
        assert!(names.contains(&n.to_string()), "expected {n} in {names:?}");
    }

    // Disable the feature and switch to macos: cache + io_uring disappear.
    req["features"]["app"] = json!([]);
    req["platform"]["os"] = json!("macos");
    let out = s.solve(&req);
    assert_eq!(out["status"], "satisfiable");
    let names: Vec<String> = out["lockfile"]["packages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["name"].as_str().unwrap().to_string())
        .collect();
    assert!(!names.contains(&"cache".to_string()));
    assert!(!names.contains(&"io_uring".to_string()));
    assert!(names.contains(&"core".to_string()));
}

#[test]
fn replay_detects_changed_inputs() {
    let s = Server::start();
    let mut req = s.fixture("diamond");
    let out = s.solve(&req);
    let lock = out["lockfile"].clone();

    // Same inputs -> exact replay.
    let ok = s.replay(json!({ "request": req, "lockfile": lock }));
    assert_eq!(ok["status"], "matches");

    // Mutate the registry (drop x 1.4.x): fingerprint changes and re-solve fails.
    req["registry"]["packages"]["x"]
        .as_array_mut()
        .unwrap()
        .retain(|v| !["1.4.0", "1.4.5"].contains(&v["version"].as_str().unwrap()));
    let drift = s.replay(json!({ "request": req, "lockfile": lock }));
    assert_eq!(drift["status"], "mismatch");
    assert_eq!(drift["mismatch"]["fingerprint_changed"], true);
    assert!(drift["mismatch"]["unsat"].is_object());
}

#[test]
fn preference_lowest_changes_selection() {
    let s = Server::start();
    let mut req = s.fixture("diamond");
    // With lowest preference, a picks 1.0.0 and x picks the lowest feasible 1.4.0.
    req["default_preference"] = json!("lowest");
    let out = s.solve(&req);
    assert_eq!(out["status"], "satisfiable");
    let by = |n: &str| {
        out["lockfile"]["packages"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["name"] == n)
            .unwrap()["version"]
            .as_str()
            .unwrap()
    };
    assert_eq!(by("x"), "1.4.0");
}

#[test]
fn pin_preference_conflicts_when_impossible() {
    let s = Server::start();
    let mut req = s.fixture("diamond");
    req["preferences"] = json!({
        "x": { "strategy": "pin", "version": "1.5.0" }
    });
    let out = s.solve(&req);
    assert_eq!(out["status"], "unsatisfiable");
    // The pin appears in the conflict trace.
    let text = out["conflict"].to_string();
    assert!(text.contains("=1.5.0"));
}

#[test]
fn unknown_package_is_unsatisfiable() {
    let s = Server::start();
    let req = json!({
        "registry": { "packages": {} },
        "requirements": [ { "name": "ghost", "req": "^1.0" } ]
    });
    let out = s.solve(&req);
    assert_eq!(out["status"], "unsatisfiable");
    assert_eq!(out["conflict"]["package"], "ghost");
}

#[test]
fn solver_and_oracle_agree_on_every_fixture() {
    // Acceptance gate: the backtracking solver's SAT/UNSAT verdict must equal
    // the brute-force enumeration on every built-in scenario.
    let s = Server::start();
    for name in ["diamond", "mutex", "cycle_ok", "cycle_unsat", "optional"] {
        let out = s.solve(&s.fixture(name));
        let expect_sat = name != "mutex" && name != "cycle_unsat";
        assert_eq!(out["status"], if expect_sat { "satisfiable" } else { "unsatisfiable" }, "fixture {name}");
        assert_eq!(out["stats"]["oracle"]["satisfiable"], expect_sat, "oracle fixture {name}");
        assert_eq!(out["stats"]["oracle"]["agrees"], true, "agreement fixture {name}");
        assert_eq!(out["stats"]["oracle"]["truncated"], false, "oracle must fully enumerate {name}");
    }
}

#[test]
fn lockfile_replay_is_deterministic_twice() {
    // Replaying the produced lockfile twice in a row must keep matching.
    let s = Server::start();
    let req = s.fixture("diamond");
    let out = s.solve(&req);
    for _ in 0..2 {
        let r = s.replay(json!({ "request": req, "lockfile": out["lockfile"] }));
        assert_eq!(r["status"], "matches");
    }
}
