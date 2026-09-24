// Rule-engine tests: acceptance scenarios plus solver-vs-exhaustive
// cross-validation on hand-built and randomized small instances.

mod common;

use common::{chosen_expressions, policy, setup};
use license_propagation::model::{Link, Strength};
use license_propagation::solve;

// The illustrative policy shared by most scenarios:
//  * MIT/Apache-2.0 selectable anywhere; GPL-2.0-only/LGPL-2.1-only too
//  * a permissive app choosing MIT can also satisfy a GPL-2.0-only incoming
//    obligation only when GPL-2.0-only lists MIT as compatible (it doesn't
//    by default), which is exactly what drives the conflict/choice cases.
fn base_policy() -> license_propagation::policy::ResolvedPolicy {
    policy(
        &[
            ("MIT", "*"),
            ("MIT", "static"),
            ("MIT", "dynamic"),
            ("Apache-2.0", "*"),
            ("Apache-2.0", "static"),
            ("Apache-2.0", "dynamic"),
            ("GPL-2.0-only", "*"),
            ("GPL-2.0-only", "static"),
            ("GPL-2.0-only", "dynamic"),
            ("LGPL-2.1-only", "*"),
            ("LGPL-2.1-only", "static"),
            ("LGPL-2.1-only", "dynamic"),
            ("ISC", "*"),
            ("ISC", "static"),
            ("ISC", "dynamic"),
        ],
        &[],
        &[],
    )
}

#[test]
fn multi_license_choice_prefers_satisfiable_alternative() {
    // app: MIT OR GPL-2.0-only, statically links a GPL-2.0 lib.
    // MIT is incompatible with the strong GPL obligation; GPL-2.0 fits.
    let p = base_policy();
    let h = setup(
        &[
            ("app", "MIT OR GPL-2.0-only"),
            ("lib", "GPL-2.0-only"),
        ],
        &[("app", "lib", Link::Static)],
        &p,
    );
    let sol = solve::backtrack(&h.prep, &h.graph, &p).expect("satisfiable");
    let picked = chosen_expressions(&h, &sol.chosen_terms);
    assert_eq!(picked, vec!["GPL-2.0-only", "GPL-2.0-only"]);
}

#[test]
fn no_alternative_means_conflict_with_path() {
    let p = base_policy();
    let h = setup(
        &[
            ("app", "MIT"),
            ("mid", "Apache-2.0"),
            ("lib", "GPL-2.0-only"),
        ],
        &[
            ("app", "mid", Link::Static),
            ("mid", "lib", Link::Static),
        ],
        &p,
    );
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());
    let witness =
        solve::copyleft_conflict_witness(&h.prep, &h.graph, &p).expect("witness");
    match witness {
        license_propagation::model::ConflictView::Copyleft {
            source,
            source_license,
            path,
            target,
            target_license,
            ..
        } => {
            assert_eq!(source, "lib");
            assert_eq!(source_license, "GPL-2.0-only");
            assert_eq!(target, "app");
            assert_eq!(target_license, "MIT");
            // path rendered top-down: app -> mid -> lib
            assert_eq!(path.len(), 2);
            assert_eq!(path[0].from, "app");
            assert_eq!(path[1].to, "lib");
        }
        other => panic!("expected copyleft witness, got {:?}", other),
    }
}

#[test]
fn exception_suppresses_emitted_obligation() {
    // GPL-2 lib WITH Classpath exception emits nothing: MIT app survives
    // even though MIT is not GPL-compatible.
    let p = base_policy();
    let h = setup(
        &[
            ("app", "MIT"),
            ("lib", "GPL-2.0-only WITH Classpath-exception-2.0"),
        ],
        &[("app", "lib", Link::Static)],
        &p,
    );
    let sol = solve::backtrack(&h.prep, &h.graph, &p).expect("satisfiable");
    assert!(sol.obligations.is_empty());
    let picked = chosen_expressions(&h, &sol.chosen_terms);
    assert_eq!(picked[0], "MIT");
}

#[test]
fn without_exception_same_graph_conflicts() {
    let p = base_policy();
    let h = setup(
        &[("app", "MIT"), ("lib", "GPL-2.0-only")],
        &[("app", "lib", Link::Static)],
        &p,
    );
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());
}

#[test]
fn unrecognized_exception_blocks_term() {
    let p = base_policy();
    let h = setup(
        &[
            ("app", "MIT"),
            ("lib", "GPL-2.0-only WITH Made-up-exception-1.0"),
        ],
        &[("app", "lib", Link::Static)],
        &p,
    );
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());
    let conflicts = solve::static_conflicts(&h.prep);
    assert!(conflicts.iter().any(|c| matches!(
        c,
        license_propagation::model::ConflictView::Allow { package, .. } if package == "lib"
    )));
}

#[test]
fn dependency_cycle_with_copyleft_fixed_point() {
    // a <-> b cycle; both GPL-2.0: obligations reach across the cycle and
    // both nodes are GPL-compatible with themselves -> satisfiable.
    let p = base_policy();
    let h = setup(
        &[
            ("a", "GPL-2.0-only"),
            ("b", "GPL-2.0-only"),
            ("c", "GPL-2.0-only"),
        ],
        &[
            ("a", "b", Link::Static),
            ("b", "a", Link::Dynamic),
            ("b", "c", Link::Static),
        ],
        &p,
    );
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_some());
    let sccs = h.graph.cyclic_sccs();
    assert_eq!(sccs.len(), 1);
    let (nodes, edges) = h.graph.cycle_in_scc(&sccs[0]);
    assert_eq!(nodes.len(), 2);
    assert_eq!(edges.len(), 2);
}

#[test]
fn cycle_with_mit_node_conflicts() {
    // a <-> b; b is GPL-2.0, a is only MIT -> strong obligation crosses the
    // cycle and MIT cannot satisfy it.
    let p = base_policy();
    let h = setup(
        &[("a", "MIT"), ("b", "GPL-2.0-only")],
        &[
            ("a", "b", Link::Static),
            ("b", "a", Link::Static),
        ],
        &p,
    );
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());
    let witness = solve::copyleft_conflict_witness(&h.prep, &h.graph, &p).unwrap();
    if let license_propagation::model::ConflictView::Copyleft {
        source,
        target,
        source_license,
        target_license,
        ..
    } = witness
    {
        assert_eq!(source, "b");
        assert_eq!(target, "a");
        assert_eq!(source_license, "GPL-2.0-only");
        assert_eq!(target_license, "MIT");
    } else {
        panic!("bad witness");
    }
}

#[test]
fn weak_copyleft_static_blocks_dynamic_escapes() {
    // LGPL weak propagates static-only by default. Dynamically linked, the
    // obligation still scopes the library itself but never reaches the app,
    // so MIT at the app is fine.
    let p = base_policy();
    let h_dyn = setup(
        &[("app", "MIT"), ("lib", "LGPL-2.1-only")],
        &[("app", "lib", Link::Dynamic)],
        &p,
    );
    let sol = solve::backtrack(&h_dyn.prep, &h_dyn.graph, &p).expect("dynamic ok");
    // obligation exists (held by lib) but the app picked MIT unconstrained
    let obs = solve::emitted_sources(&sol);
    assert_eq!(obs.len(), 1);
    assert_eq!(h_dyn.ids[obs[0].source], "lib");
    assert_eq!(
        chosen_expressions(&h_dyn, &sol.chosen_terms)[0],
        "MIT".to_string()
    );

    let h_st = setup(
        &[("app", "MIT"), ("lib", "LGPL-2.1-only")],
        &[("app", "lib", Link::Static)],
        &p,
    );
    assert!(solve::backtrack(&h_st.prep, &h_st.graph, &p).is_none());
}

#[test]
fn and_term_must_be_pairwise_compatible() {
    // Package offers (MIT AND GPL-2.0-only) as one conjunction; the GPL
    // obligation scopes the package itself and MIT is incompatible.
    let p = base_policy();
    let h = setup(
        &[("app", "ISC OR (MIT AND GPL-2.0-only)")],
        &[],
        &p,
    );
    let sol = solve::backtrack(&h.prep, &h.graph, &p).expect("sat via ISC");
    assert_eq!(
        chosen_expressions(&h, &sol.chosen_terms),
        vec!["ISC".to_string()]
    );

    // Without the ISC escape it must be infeasible.
    let h2 = setup(&[("app", "MIT AND GPL-2.0-only")], &[], &p);
    assert!(solve::backtrack(&h2.prep, &h2.graph, &p).is_none());
}

#[test]
fn root_context_requires_star_entry() {
    // ISC only allowed behind static edges; a root ISC package is rejected.
    let p = policy(
        &[("MIT", "*"), ("ISC", "static")],
        &[],
        &[],
    );
    let h = setup(&[("app", "ISC")], &[], &p);
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());
    let conflicts = solve::static_conflicts(&h.prep);
    assert!(matches!(
        &conflicts[0],
        license_propagation::model::ConflictView::Allow { context, .. } if context == "*"
    ));
}

#[test]
fn unknown_license_rejected_by_default_allowed_on_request() {
    // WeirdLicense is not in the allow matrix nor in extra_licenses: rejected.
    let p = policy(&[], &[], &[]);
    let h = setup(&[("app", "WeirdLicense")], &[], &p);
    assert!(solve::backtrack(&h.prep, &h.graph, &p).is_none());

    // Declared both known and allowed: accepted as non-copyleft.
    let p2 = policy(&[("WeirdLicense", "*")], &[], &["WeirdLicense"]);
    let h2 = setup(&[("app", "WeirdLicense")], &[], &p2);
    assert!(solve::backtrack(&h2.prep, &h2.graph, &p2).is_some());
}

#[test]
fn custom_compatibility_matrix_resolves_conflict() {
    // Project declares GPL-2.0-only compatible with MIT -> conflict gone.
    let p = policy(
        &[
            ("MIT", "*"),
            ("MIT", "static"),
            ("MIT", "dynamic"),
            ("GPL-2.0-only", "*"),
            ("GPL-2.0-only", "static"),
            ("GPL-2.0-only", "dynamic"),
        ],
        &[("GPL-2.0-only", "MIT")],
        &[],
    );
    let h = setup(
        &[("app", "MIT"), ("lib", "GPL-2.0-only")],
        &[("app", "lib", Link::Static)],
        &p,
    );
    let sol = solve::backtrack(&h.prep, &h.graph, &p).expect("matrix fixes it");
    let obs = solve::emitted_sources(&sol);
    assert_eq!(obs.len(), 1);
    assert_eq!(obs[0].strength, Strength::Strong);
}

#[test]
fn exhaustive_oracle_matches_solver_on_scenarios() {
    let p = base_policy();
    let mk = |pkgs: &[(&str, &str)], edges: &[(&str, &str, Link)]| {
        setup(pkgs, edges, &p)
    };
    let cases: Vec<(
        license_propagation::solve::Prepared,
        license_propagation::graph::Graph,
        bool,
    )> = vec![
        {
            let h = mk(
                &[("a", "MIT OR GPL-2.0-only"), ("b", "GPL-2.0-only")],
                &[("a", "b", Link::Static)],
            );
            let sat = solve::backtrack(&h.prep, &h.graph, &p).is_some();
            (h.prep, h.graph, sat)
        },
        {
            let h = mk(
                &[("a", "MIT"), ("b", "GPL-2.0-only")],
                &[("a", "b", Link::Static)],
            );
            let sat = solve::backtrack(&h.prep, &h.graph, &p).is_some();
            (h.prep, h.graph, sat)
        },
        {
            let h = mk(
                &[
                    ("a", "MIT OR Apache-2.0"),
                    ("b", "MIT OR LGPL-2.1-only"),
                    ("c", "GPL-2.0-only"),
                ],
                &[
                    ("a", "b", Link::Dynamic),
                    ("b", "c", Link::Static),
                ],
            );
            let sat = solve::backtrack(&h.prep, &h.graph, &p).is_some();
            (h.prep, h.graph, sat)
        },
    ];
    for (prep, graph, sat) in cases {
        let enumr = solve::enumerate(&prep, &graph, &p, 100_000);
        assert_eq!(enumr.feasible > 0, sat);
    }
}

// Deterministic PRNG so the randomized cross-check is reproducible.
struct Lcg(u64);
impl Lcg {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

#[test]
fn randomized_small_instances_solver_matches_enumeration() {
    // 3-4 nodes, licenses drawn from a fixed pool with random OR terms and
    // random edges/links; the exhaustive oracle is the reference.
    let licenses = ["MIT", "Apache-2.0", "ISC", "GPL-2.0-only", "LGPL-2.1-only"];
    let p = {
        let mut allowed = Vec::new();
        for l in licenses {
            allowed.push((l, "*"));
            allowed.push((l, "static"));
            allowed.push((l, "dynamic"));
        }
        policy(&allowed, &[], &[])
    };

    let mut rng = Lcg(0xC0FFEEu64);
    let mut checked = 0;
    for _ in 0..400 {
        let n = 3 + rng.below(2); // 3..=4
        let mut pkgs: Vec<(String, String)> = Vec::new();
        for i in 0..n {
            let mut opts = Vec::new();
            let k = 1 + rng.below(2); // 1..=2 alternatives
            for _ in 0..k {
                opts.push(licenses[rng.below(licenses.len())].to_string());
            }
            let spdx = opts.join(" OR ");
            pkgs.push((format!("n{}", i), spdx));
        }
        let mut edges: Vec<(String, String, Link)> = Vec::new();
        for i in 0..n {
            for j in 0..n {
                if i != j && rng.below(100) < 35 {
                    let link = if rng.below(2) == 0 {
                        Link::Static
                    } else {
                        Link::Dynamic
                    };
                    edges.push((format!("n{}", i), format!("n{}", j), link));
                }
            }
        }
        let refs: Vec<(&str, &str)> = pkgs
            .iter()
            .map(|(id, s)| (id.as_str(), s.as_str()))
            .collect();
        let erefs: Vec<(&str, &str, Link)> = edges
            .iter()
            .map(|(a, b, l)| (a.as_str(), b.as_str(), *l))
            .collect();
        let h = setup(&refs, &erefs, &p);
        let solver_sat = solve::backtrack(&h.prep, &h.graph, &p).is_some();
        let oracle = solve::enumerate(&h.prep, &h.graph, &p, 1_000_000);
        assert_eq!(
            solver_sat,
            oracle.feasible > 0,
            "solver/exhaustive mismatch on pkgs={:?} edges={:?}",
            refs,
            erefs
        );
        checked += 1;
    }
    assert!(checked >= 400);
}
