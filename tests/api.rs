//! End-to-end scenario tests through the public `evaluate` entry point.
//!
//! These cover the acceptance criteria: multi-license OR-choice, WITH
//! exceptions, dependency cycles, conflict paths and the custom allow matrix.

use serde_json::{json, Value};

use license_propagation::{evaluate, EvaluateRequest};

fn run(v: Value) -> Value {
    let req: EvaluateRequest =
        serde_json::from_value(v).expect("request must deserialize");
    serde_json::to_value(evaluate(&req).expect("evaluation must succeed")).unwrap()
}

fn pkg(id: &str, license: &str) -> Value {
    json!({"id": id, "license": license})
}

fn edge(from: &str, to: &str, linking: &str) -> Value {
    json!({"from": from, "to": to, "linking": linking})
}

fn graph(root: &str, packages: Vec<Value>, edges: Vec<Value>) -> Value {
    json!({"root": root, "packages": packages, "edges": edges})
}

fn choice_of<'a>(resp: &'a Value, id: &str) -> &'a str {
    resp["selection"][id]["expression"].as_str().unwrap()
}

#[test]
fn multi_license_or_choice_prefers_permissive() {
    // Proprietary app statically links a library offering MIT or GPL-3.0.
    // GPL is blocked by the copyleft-into-proprietary rule; MIT wins.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "Proprietary"),
                pkg("lib", "MIT OR GPL-3.0-only"),
            ],
            vec![edge("app", "lib", "static")],
        )
    }));
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(choice_of(&resp, "lib"), "MIT");
    assert_eq!(resp["violations"].as_array().unwrap().len(), 0);
}

#[test]
fn all_copyleft_choices_conflict_with_proprietary_root() {
    // No permissive alternative: conflict path must point app -> lib.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "Proprietary"),
                pkg("lib", "GPL-2.0-only OR GPL-3.0-only"),
            ],
            vec![edge("app", "lib", "static")],
        )
    }));
    assert_eq!(resp["satisfiable"], false);
    assert!(resp["selection"].is_null());
    let kinds: Vec<&str> = resp["unavoidableViolations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["kind"].as_str().unwrap())
        .collect();
    assert!(
        kinds.contains(&"copyleft-into-proprietary"),
        "expected copyleft-into-proprietary, got {kinds:?}"
    );
    let v = resp["unavoidableViolations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["kind"] == "copyleft-into-proprietary")
        .unwrap();
    assert_eq!(v["path"], json!(["app", "lib"]));
}

#[test]
fn with_classpath_exception_clears_copyleft() {
    // GPL-3.0 WITH Classpath-exception-2.0 may be linked into proprietary code.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "Proprietary"),
                pkg("lib", "GPL-3.0-only WITH Classpath-exception-2.0"),
            ],
            vec![edge("app", "lib", "static")],
        )
    }));
    assert_eq!(resp["satisfiable"], true);
    assert!(
        choice_of(&resp, "lib").contains("Classpath-exception-2.0"),
        "got {}",
        resp
    );
}

#[test]
fn unknown_exception_is_rejected_by_default_but_allowed_when_configured() {
    let req = json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "Proprietary"),
                pkg("lib", "GPL-3.0-only WITH SomeCustomException"),
            ],
            vec![edge("app", "lib", "static")],
        )
    });
    let resp = run(req.clone());
    assert_eq!(resp["satisfiable"], false);
    assert_eq!(
        resp["unavoidableViolations"][0]["kind"], "not-allowed",
        "got {resp}"
    );

    let mut with_policy = req;
    with_policy["policy"]["unknownExceptionsAllowed"] = json!(true);
    with_policy["policy"]["exceptions"]["SomeCustomException"] = json!({"effect": "clear"});
    let resp2 = run(with_policy);
    assert_eq!(resp2["satisfiable"], true, "got {resp2}");
}

#[test]
fn license_incompatibility_matrix_forces_alternative() {
    // app is GPL-3.0 statically linked to a library offered under
    // GPL-2.0-only or MIT. GPL-2.0 is declared incompatible with GPL-3.0;
    // MIT must be chosen.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "GPL-3.0-only"),
                pkg("lib", "GPL-2.0-only OR MIT"),
            ],
            vec![edge("app", "lib", "static")],
        )
    }));
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(choice_of(&resp, "lib"), "MIT");
}

#[test]
fn two_node_cycle_is_satisfiable_with_compatible_licenses() {
    // a -> b -> a, both permissive: fixed point converges.
    let resp = run(json!({
        "graph": graph(
            "a",
            vec![
                pkg("a", "MIT OR GPL-3.0-only"),
                pkg("b", "Apache-2.0 OR GPL-2.0-only"),
            ],
            vec![edge("a", "b", "static"), edge("b", "a", "static")],
        )
    }));
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(resp["cycles"][0], json!(["a", "b", "a"]));
    assert_eq!(choice_of(&resp, "a"), "MIT");
    assert_eq!(choice_of(&resp, "b"), "Apache-2.0");
}

#[test]
fn three_node_cycle_unavoidable_ceiling_conflict() {
    // Cycle x -> y -> z -> x, all static. z is GPL-3.0-only; its strong
    // burden travels around the cycle to x, whose license is permissive, so
    // no assignment can satisfy x's ceiling. y still has an OR choice, which
    // must not hide the unavoidable conflict path.
    let resp = run(json!({
        "graph": graph(
            "x",
            vec![
                pkg("x", "MIT OR ISC"),
                pkg("y", "Apache-2.0 OR GPL-3.0-only"),
                pkg("z", "GPL-3.0-only"),
            ],
            vec![
                edge("x", "y", "static"),
                edge("y", "z", "static"),
                edge("z", "x", "static"),
            ],
        )
    }));
    assert_eq!(resp["satisfiable"], false, "got {resp}");
    let kinds: Vec<&str> = resp["unavoidableViolations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["kind"].as_str().unwrap())
        .collect();
    assert!(
        kinds.contains(&"root-copyleft-ceiling"),
        "expected root ceiling conflict through the cycle, got {kinds:?}: {resp}"
    );
    // Cycle reported in the response.
    let cycles = resp["cycles"].as_array().unwrap();
    assert_eq!(cycles[0], json!(["x", "y", "z", "x"]));
}

#[test]
fn dynamic_link_copyleft_is_configurable() {
    // LGPL weak copyleft reaches a proprietary app over a dynamic edge.
    // Default conservative policy flags it; turning dynamic checks off
    // allows it; static edges are always blocked for weak copyleft unless
    // proprietaryRejectsWeak is also relaxed.
    let base = json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "Proprietary"),
                pkg("lib", "LGPL-2.1-only"),
            ],
            vec![edge("app", "lib", "dynamic")],
        )
    });
    // Conservative default: weak burden still rejected on a dynamic edge.
    assert_eq!(run(base.clone())["satisfiable"], false);

    // Allowing weak copyleft across dynamic links fixes this case.
    let mut dyn_ok = base.clone();
    dyn_ok["policy"]["checkDynamicLinks"] = json!(false);
    assert_eq!(run(dyn_ok)["satisfiable"], true);

    // A static edge stays blocked even with dynamic checks switched off.
    let mut static_blocked = base.clone();
    static_blocked["policy"]["checkDynamicLinks"] = json!(false);
    static_blocked["graph"]["edges"][0]["linking"] = json!("static");
    assert_eq!(run(static_blocked)["satisfiable"], false);

    // Relaxing the proprietary weak rule permits the static case too.
    let mut relaxed = base;
    relaxed["policy"]["checkDynamicLinks"] = json!(false);
    relaxed["policy"]["proprietaryRejectsWeak"] = json!(false);
    relaxed["graph"]["edges"][0]["linking"] = json!("static");
    assert_eq!(run(relaxed)["satisfiable"], true);
}

#[test]
fn custom_allow_matrix_denies_a_builtin_license() {
    let mut denied = json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "MIT OR GPL-3.0-only"),
            ],
            vec![],
        ),
        "policy": {"denied": ["MIT"]}
    });
    // GPL-3.0 selected: root ceiling = strong, so it is consistent.
    let resp = run(denied.clone());
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(choice_of(&resp, "app"), "GPL-3.0-only");

    // Denying both leaves no valid alternative.
    denied["policy"]["denied"] = json!(["MIT", "GPL-3.0-only"]);
    let resp = run(denied);
    assert_eq!(resp["satisfiable"], false);
    assert_eq!(
        resp["unavoidableViolations"][0]["kind"], "not-allowed",
        "got {resp}"
    );
}

#[test]
fn custom_license_classification_and_incompatibility() {
    // A project-specific license plus a custom incompatible pair. Both sides
    // are permissive, so the custom pair is the only deciding constraint.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "WeirdLicense-1.0 OR MIT"),
                pkg("lib", "ISC"),
            ],
            vec![edge("app", "lib", "static")],
        ),
        "policy": {
            "allowed": ["WeirdLicense-1.0"],
            "licenses": {"WeirdLicense-1.0": {"copyleft": "none"}},
            "incompatiblePairs": [["WeirdLicense-1.0", "ISC"]]
        }
    }));
    // WeirdLicense is incompatible with ISC lib, so MIT is picked.
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(choice_of(&resp, "app"), "MIT");
}

#[test]
fn enumerates_all_satisfying_selections_on_request() {
    // Two independent binary choices, all permissive -> 4 selections.
    let resp = run(json!({
        "graph": graph(
            "app",
            vec![
                pkg("app", "MIT OR ISC"),
                pkg("lib", "MIT OR Apache-2.0"),
            ],
            vec![],
        ),
        "enumerate": 100
    }));
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(resp["enumeration"]["totalAlternatives"], 4);
    assert_eq!(resp["enumeration"]["complete"], true);
    assert_eq!(resp["enumeration"]["satisfyingSelections"].as_array().unwrap().len(), 4);
}

#[test]
fn invalid_graph_inputs_return_errors() {
    let bad = |v: Value| -> String {
        let req: EvaluateRequest = serde_json::from_value(v).unwrap();
        evaluate(&req).unwrap_err()
    };
    assert!(bad(json!({"graph": graph("missing", vec![pkg("a", "MIT")], vec![])}))
        .contains("root package"));
    assert!(bad(json!({
        "graph": graph("a",
            vec![pkg("a", "MIT"), pkg("a", "ISC")], vec![])
    }))
    .contains("duplicate package id"));
    assert!(bad(json!({
        "graph": graph("a",
            vec![pkg("a", "MIT AND")], vec![])
    }))
    .contains("invalid SPDX expression"));
    assert!(bad(json!({
        "graph": graph("a",
            vec![pkg("a", "MIT")],
            vec![edge("a", "ghost", "static")])
    }))
    .contains("unknown package"));
}

#[test]
fn every_response_carries_the_no_legal_advice_disclaimer() {
    let resp = run(json!({"graph": graph("a", vec![pkg("a", "MIT")], vec![])}));
    assert!(resp["disclaimer"].as_str().unwrap().contains("NOT legal advice"));
}
