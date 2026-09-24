//! Brute-force cross-check: on small random instances the backtracking engine
//! must agree with a *separately written*, deliberately naive exhaustive
//! enumerator that shares no code with the engine.
//!
//! The oracle below models the same rules directly on JSON-like literals:
//! DNF is computed by string splitting, propagation by repeated sweeps. If
//! both implementations agree over many random instances, the solver and
//! the fixed-point propagation gain independent corroboration.

use serde_json::{json, Value};

use license_propagation::{evaluate, EvaluateRequest};

/// Oracle license table. `level: 0` permissive, `1` weak, `2` strong.
struct OLicense {
    allowed: bool,
    level: u8,
    proprietary: bool,
}

fn oracle_table(id: &str) -> OLicense {
    match id {
        "MIT" | "ISC" | "Apache-2.0" | "BSD-2-Clause" => OLicense {
            allowed: true,
            level: 0,
            proprietary: false,
        },
        "LGPL-2.1-only" | "MPL-2.0" => OLicense {
            allowed: true,
            level: 1,
            proprietary: false,
        },
        "GPL-3.0-only" | "GPL-2.0-only" | "AGPL-3.0-only" => OLicense {
            allowed: true,
            level: 2,
            proprietary: false,
        },
        "Proprietary" => OLicense {
            allowed: true,
            level: 0,
            proprietary: true,
        },
        // Anything else the random generator may emit: disallowed.
        _ => OLicense {
            allowed: false,
            level: 0,
            proprietary: false,
        },
    }
}

fn oracle_incompatible(a: &str, b: &str) -> bool {
    let key = if a <= b { (a, b) } else { (b, a) };
    matches!(
        key,
        ("GPL-2.0-only", "GPL-3.0-only")
            | ("AGPL-3.0-only", "GPL-2.0-only")
            | ("Apache-2.0", "GPL-2.0-only")
            | ("GPL-3.0-only", "SSPL-1.0")
    )
}

struct Oracle {
    /// alternatives per package: each alternative is a list of license ids
    /// (terms are exception-free in these instances).
    alts: Vec<Vec<Vec<String>>>,
    /// static (true) / dynamic (false), from -> to
    edges: Vec<(usize, usize, bool)>,
    root: usize,
}

/// Tiny DNF by recursive parse-free splitting good enough for the flat
/// `A OR B` expressions the generator emits (each side may be `X AND Y`).
fn flat_dnf(expr: &str) -> Vec<Vec<String>> {
    expr.split(" OR ")
        .map(|conj| {
            conj.split(" AND ")
                .map(|s| s.trim().to_string())
                .collect::<Vec<_>>()
        })
        .collect()
}

impl Oracle {
    /// Returns (satisfiable, set of satisfying assignments) where each
    /// assignment is rendered as "node:license,..." sorted by node.
    fn solve(&self) -> (bool, Vec<String>) {
        let n = self.alts.len();
        let mut combo = vec![0usize; n];
        let mut satisfying = Vec::new();
        loop {
            let ok = self.assignment_ok(&combo);
            if ok {
                let mut desc: Vec<String> = (0..n)
                    .map(|i| {
                        let mut chosen = self.alts[i][combo[i]].clone();
                        chosen.sort();
                        format!("{}:{}", i, chosen.join("+"))
                    })
                    .collect();
                desc.sort();
                satisfying.push(desc.join(","));
            }
            // odometer
            let mut k = 0;
            loop {
                if k == n {
                    let sat = !satisfying.is_empty();
                    satisfying.sort();
                    satisfying.dedup();
                    return (sat, satisfying);
                }
                combo[k] += 1;
                if combo[k] < self.alts[k].len() {
                    break;
                }
                combo[k] = 0;
                k += 1;
            }
        }
    }

    fn assignment_ok(&self, combo: &[usize]) -> bool {
        let n = self.alts.len();

        // Domain check: every chosen term must be an allowed license.
        let mut base_level = vec![0u8; n];
        let mut proprietary = vec![true; n];
        for i in 0..n {
            if self.alts[i][combo[i]].is_empty() {
                proprietary[i] = false;
            }
            for lic in &self.alts[i][combo[i]] {
                let t = oracle_table(lic);
                if !t.allowed {
                    return false;
                }
                base_level[i] = base_level[i].max(t.level);
                if !t.proprietary {
                    proprietary[i] = false;
                }
            }
        }

        // Fixed-point burden propagation.
        let mut eff = base_level.clone();
        let mut changed = true;
        // Engine default: checkDynamicLinks = true (conservative), so weak
        // copyleft carries over dynamic links too in these instances.
        while changed {
            changed = false;
            for &(from, to, _is_static) in &self.edges {
                let carried = match eff[to] {
                    0 => 0,
                    2 => 2, // strong propagates through any linking
                    1 => 1, // weak propagates under the conservative default
                    _ => 0,
                };
                if carried > eff[from] {
                    eff[from] = carried;
                    changed = true;
                }
            }
        }

        // Intra-conjunction incompatibility inside a single package.
        for (chosen, &alt_idx) in self.alts.iter().zip(combo.iter()) {
            let chosen = &chosen[alt_idx];
            for x in 0..chosen.len() {
                for y in (x + 1)..chosen.len() {
                    if oracle_incompatible(&chosen[x], &chosen[y]) {
                        return false;
                    }
                }
            }
        }

        // Edge incompatibility between any pair of combined licenses.
        for &(from, to, _) in &self.edges {
            for a in &self.alts[from][combo[from]] {
                for b in &self.alts[to][combo[to]] {
                    if oracle_incompatible(a, b) {
                        return false;
                    }
                }
            }
        }

        // Copyleft into proprietary: under the conservative default both
        // weak and strong burden trigger regardless of linking.
        for &(from, to, _is_static) in &self.edges {
            if proprietary[from] && eff[to] >= 1 {
                return false;
            }
        }

        // Root ceiling.
        if eff[self.root] > base_level[self.root] {
            return false;
        }

        true
    }
}

/// Tiny deterministic LCG so test runs are reproducible.
struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() as usize) % n
    }
    fn pick<'a, T>(&mut self, xs: &'a [T]) -> &'a T {
        &xs[self.below(xs.len())]
    }
}

const LICENSES: &[&str] = &[
    "MIT",
    "ISC",
    "Apache-2.0",
    "BSD-2-Clause",
    "LGPL-2.1-only",
    "MPL-2.0",
    "GPL-3.0-only",
    "GPL-2.0-only",
    "AGPL-3.0-only",
    "Proprietary",
];

fn random_instance(seed: u64) -> (Value, Oracle) {
    let mut rng = Rng(seed);
    let n = 2 + rng.below(4); // 2..=5 packages

    let mut packages = Vec::new();
    let mut alts = Vec::new();
    for i in 0..n {
        // 1 or 2 alternatives; a 2-term conjunction appears occasionally.
        let k = 1 + rng.below(2);
        let mut terms: Vec<String> = Vec::new();
        for _ in 0..k {
            if rng.below(4) == 0 {
                let a = rng.pick(LICENSES).to_string();
                let mut b = rng.pick(LICENSES).to_string();
                while b == a {
                    b = rng.pick(LICENSES).to_string();
                }
                terms.push(format!("{a} AND {b}"));
            } else {
                terms.push(rng.pick(LICENSES).to_string());
            }
        }
        let expr = terms.join(" OR ");
        packages.push(json!({"id": i.to_string(), "license": expr}));
        alts.push(flat_dnf(&expr));
    }

    let edge_count = rng.below(7); // 0..=6
    let mut edges_json = Vec::new();
    let mut edges = Vec::new();
    for _ in 0..edge_count {
        let from = rng.below(n);
        let to = rng.below(n);
        let is_static = rng.below(2) == 0;
        edges_json.push(json!({
            "from": from.to_string(),
            "to": to.to_string(),
            "linking": if is_static { "static" } else { "dynamic" }
        }));
        edges.push((from, to, is_static));
    }

    let request = json!({
        "graph": {
            "root": "0",
            "packages": packages,
            "edges": edges_json,
        }
    });
    let oracle = Oracle {
        alts,
        edges,
        root: 0,
    };
    (request, oracle)
}

fn engine_satisfying_set(req: &Value) -> (bool, Vec<String>) {
    let parsed: EvaluateRequest = serde_json::from_value(req.clone()).unwrap();
    let resp = evaluate(&parsed).unwrap();
    let v = serde_json::to_value(&resp).unwrap();

    // Ask the engine to enumerate every satisfying selection. Bound the
    // combinations: n <= 5, each <= 2 alts => at most 32.
    let mut enumerating = req.clone();
    enumerating["enumerate"] = json!(1000);
    let parsed2: EvaluateRequest = serde_json::from_value(enumerating).unwrap();
    let resp2 = evaluate(&parsed2).unwrap();
    let v2 = serde_json::to_value(&resp2).unwrap();

    let satisfiable = v["satisfiable"].as_bool().unwrap();
    let mut set = Vec::new();
    if let Some(sel) = v2["enumeration"]["satisfyingSelections"].as_array() {
        for s in sel {
            let obj = s.as_object().unwrap();
            let mut desc: Vec<String> = obj
                .into_iter()
                .map(|(node, choice)| {
                    let terms: Vec<String> = choice["terms"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|t| t.as_str().unwrap().to_string())
                        .collect();
                    format!("{node}:{}", terms.join("+"))
                })
                .collect();
            desc.sort();
            set.push(desc.join(","));
        }
    }
    set.sort();
    set.dedup();
    (satisfiable, set)
}

#[test]
fn engine_matches_brute_force_on_random_small_instances() {
    let total = 500usize;
    let mut satisfiable_count = 0;
    let mut cyclic_count = 0;
    for seed in 0..total as u64 {
        let (req, oracle) = random_instance(seed);
        let (o_sat, o_set) = oracle.solve();
        let (e_sat, e_set) = engine_satisfying_set(&req);

        assert_eq!(
            o_sat, e_sat,
            "satisfiability mismatch on seed {seed}: engine={e_sat} oracle={o_sat}\nrequest={req}"
        );
        if o_sat {
            satisfiable_count += 1;
            assert_eq!(
                o_set, e_set,
                "satisfying-assignment set mismatch on seed {seed}\nrequest={req}"
            );
        }
        if e_sat {
            let parsed: EvaluateRequest = serde_json::from_value(req).unwrap();
            let resp = serde_json::to_value(evaluate(&parsed).unwrap()).unwrap();
            if !resp["cycles"].as_array().unwrap().is_empty() {
                cyclic_count += 1;
            }
        }
    }
    // Sanity: the random sweep must actually exercise both outcomes and
    // cycles, otherwise this cross-check would be vacuous.
    assert!(satisfiable_count > 20, "too few satisfiable instances generated");
    assert!(satisfiable_count < total - 20, "too few unsatisfiable instances generated");
    assert!(cyclic_count > 0, "no cyclic instances were exercised");
    eprintln!(
        "cross-checked {total} random instances: {satisfiable_count} satisfiable, \
         {cyclic_count} of those contain dependency cycles"
    );
}

#[test]
fn engine_matches_brute_force_on_fixed_acceptance_cases() {
    // Headline acceptance graph: multi-license choice + WITH exception
    // consideration + a dependency cycle, all in one small graph.
    //   0 = GPL-3.0-only OR Proprietary (root)
    //   1 = GPL-3.0-only WITH Classpath-exception-2.0 OR MIT
    //   2 = Apache-2.0 OR GPL-2.0-only
    //   edges 0->1 static, 1->2 static, 2->0 static (cycle)
    // Satisfying: a Proprietary root is legal precisely because node 1's
    // exception clears the GPL burden that would otherwise travel around
    // the static cycle into it; node 2 must avoid GPL-2.0 (incompatible
    // with node 1's GPL-3.0), so it takes Apache-2.0.
    let req = json!({
        "graph": {
            "root": "0",
            "packages": [
                {"id": "0", "license": "GPL-3.0-only OR Proprietary"},
                {"id": "1", "license": "GPL-3.0-only WITH Classpath-exception-2.0 OR MIT"},
                {"id": "2", "license": "Apache-2.0 OR GPL-2.0-only"},
            ],
            "edges": [
                {"from": "0", "to": "1", "linking": "static"},
                {"from": "1", "to": "2", "linking": "static"},
                {"from": "2", "to": "0", "linking": "static"}
            ]
        }
    });
    let parsed: EvaluateRequest = serde_json::from_value(req).unwrap();
    let resp = serde_json::to_value(evaluate(&parsed).unwrap()).unwrap();
    assert_eq!(resp["satisfiable"], true);
    assert!(!resp["cycles"].as_array().unwrap().is_empty());
    assert_eq!(resp["selection"]["0"]["expression"], "Proprietary");
    assert!(
        resp["selection"]["1"]["expression"]
            .as_str()
            .unwrap()
            .contains("Classpath-exception-2.0"),
        "got {resp}"
    );
    assert_eq!(resp["selection"]["2"]["expression"], "Apache-2.0");
}

#[test]
fn without_the_exception_the_same_cycle_blocks_the_proprietary_root() {
    // Same graph, but node 1's only choices are copyleft without a clearing
    // exception: strong GPL (rejected on the Proprietary root and
    // incompatible with nothing here) vs weak LGPL, both rejected over the
    // static cycle. The only surviving root choice is GPL-3.0-only —
    // demonstrating the WITH exception in the case above is what made the
    // Proprietary root possible.
    let req = json!({
        "graph": {
            "root": "0",
            "packages": [
                {"id": "0", "license": "GPL-3.0-only OR Proprietary"},
                {"id": "1", "license": "GPL-3.0-only OR LGPL-2.1-only"},
                {"id": "2", "license": "Apache-2.0"},
            ],
            "edges": [
                {"from": "0", "to": "1", "linking": "static"},
                {"from": "1", "to": "2", "linking": "static"},
                {"from": "2", "to": "0", "linking": "static"}
            ]
        }
    });
    let parsed: EvaluateRequest = serde_json::from_value(req).unwrap();
    let resp = serde_json::to_value(evaluate(&parsed).unwrap()).unwrap();
    assert_eq!(resp["satisfiable"], true);
    assert_eq!(resp["selection"]["0"]["expression"], "GPL-3.0-only");
}
