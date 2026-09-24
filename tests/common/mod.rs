// Test helpers: build a Prepared/Graph/Policy directly from concise inputs.

#![allow(dead_code)]

use std::collections::BTreeMap;

use license_propagation::graph::Graph;
use license_propagation::model::{Link, PolicyReq};
use license_propagation::policy::ResolvedPolicy;
use license_propagation::solve::{self, Prepared};
use license_propagation::spdx;

pub struct Harness {
    pub ids: Vec<String>,
    pub graph: Graph,
    pub policy: ResolvedPolicy,
    pub prep: Prepared,
}

pub fn policy(
    allowed: &[(&str, &str)],
    compatible: &[(&str, &str)],
    extras: &[&str],
) -> ResolvedPolicy {
    let mut m: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (lic, ctxs) in allowed {
        m.entry(lic.to_string())
            .or_default()
            .push(ctxs.to_string());
    }
    let mut cw: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (obl, tgt) in compatible {
        cw.entry(obl.to_string())
            .or_default()
            .push(tgt.to_string());
    }
    let req = PolicyReq {
        allowed_licenses: m,
        extra_licenses: extras.iter().map(|s| s.to_string()).collect(),
        exceptions: Vec::new(),
        copyleft: BTreeMap::new(),
        compatible_with: cw,
        strong_propagates_on: None,
        weak_propagates_on: None,
        unknown_licenses: Default::default(),
    };
    ResolvedPolicy::resolve(&req).unwrap()
}

pub fn setup(
    pkgs: &[(&str, &str)],
    edges: &[(&str, &str, Link)],
    policy: &ResolvedPolicy,
) -> Harness {
    let ids: Vec<String> = pkgs.iter().map(|(id, _)| id.to_string()).collect();
    let spdx_texts: Vec<String> = pkgs.iter().map(|(_, s)| s.to_string()).collect();
    let exprs: Vec<spdx::SpdxExpr> = spdx_texts
        .iter()
        .map(|s| spdx::parse(s).unwrap())
        .collect();
    let raw: Vec<(String, String, Link)> = edges
        .iter()
        .map(|(a, b, l)| (a.to_string(), b.to_string(), *l))
        .collect();
    let graph = Graph::build(&ids, &raw).unwrap();
    let prep = solve::prepare(&ids, &spdx_texts, exprs, &graph, policy).unwrap();
    Harness {
        ids,
        graph,
        policy: policy.clone(),
        prep,
    }
}

pub fn chosen_expressions(h: &Harness, terms: &[usize]) -> Vec<String> {
    h.prep
        .nodes
        .iter()
        .enumerate()
        .map(|(i, n)| {
            let t = &n.expr.dnf[terms[i]];
            t.atoms
                .iter()
                .map(|a| a.display())
                .collect::<Vec<_>>()
                .join(" AND ")
        })
        .collect()
}
