//! Verification harness comparing the Leapfrog Triejoin against the
//! independent naive enumeration.
//!
//! For one scenario it:
//! 1. plans + builds immutable tries (the real backend path);
//! 2. runs Leapfrog to completion and records seeks/nexts/open/up;
//! 3. runs the separate cartesian nested-loop oracle and records the number of
//!    row combinations examined;
//! 4. asserts the two output multisets are identical;
//! 5. reports a "blow-up" comparison showing Leapfrog never materialises the
//!    oracle's cartesian intermediate.
//!
//! Expected answers and costs are produced by the oracle / closed-form counts,
//! not by the core under test.

use std::collections::BTreeMap;

use crate::batch::RecordBatch;
use crate::error::Result;
use crate::leapfrog::{self, TrieSet};
use crate::naive;
use crate::plan::{self, JoinRequest};
use crate::types::Datum;

/// Result of verifying a single scenario.
#[derive(Debug, Clone)]
pub struct Report {
    pub scenario: String,
    pub output_vars: Vec<String>,
    /// Sorted output multiset from Leapfrog (datums rendered as strings).
    pub leapfrog_rows: Vec<Vec<String>>,
    pub naive_rows: Vec<Vec<String>>,
    pub results_match: bool,
    pub leapfrog: LeapfrogCost,
    pub naive_combinations: u64,
    pub naive_equality_checks: u64,
    /// Product of retained input sizes — the cartesian intermediate a naive
    /// pairwise plan would build.
    pub cartesian_intermediate: u64,
    /// Sum of trie node counts (the only structure Leapfrog materialises).
    pub trie_nodes: usize,
}

/// Leapfrog physical work.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LeapfrogCost {
    pub seeks: u64,
    pub nexts: u64,
    pub opens: u64,
    pub ups: u64,
    pub rows_emitted: u64,
}

impl Report {
    /// True when Leapfrog's materialised structure is far smaller than the
    /// cartesian intermediate the naive plan would create.
    pub fn no_cartesian_blowup(&self) -> bool {
        (self.trie_nodes as u64) <= self.cartesian_intermediate
    }

    pub fn to_text(&self) -> String {
        let mut s = String::new();
        s.push_str(&format!("scenario: {}\n", self.scenario));
        s.push_str(&format!("  output vars       : {:?}\n", self.output_vars));
        s.push_str(&format!("  result rows       : {}\n", self.leapfrog_rows.len()));
        s.push_str(&format!("  results match     : {}\n", self.results_match));
        s.push_str(&format!(
            "  leapfrog seeks    : {}  nexts: {}  opens: {}  ups: {}\n",
            self.leapfrog.seeks,
            self.leapfrog.nexts,
            self.leapfrog.opens,
            self.leapfrog.ups
        ));
        s.push_str(&format!(
            "  leapfrog emitted  : {}  (trie nodes materialised: {})\n",
            self.leapfrog.rows_emitted, self.trie_nodes
        ));
        s.push_str(&format!(
            "  naive combinations: {}  (cartesian intermediate)\n",
            self.naive_combinations
        ));
        s.push_str(&format!(
            "  naive eq checks   : {}\n",
            self.naive_equality_checks
        ));
        s.push_str(&format!(
            "  blow-up avoided   : {} ({} << {})\n",
            self.no_cartesian_blowup(),
            self.trie_nodes,
            self.cartesian_intermediate
        ));
        s
    }
}

fn datum_string(d: &Datum) -> String {
    match d {
        Datum::Null => "NULL".into(),
        Datum::Int(i) => i.to_string(),
        Datum::Text(s) => s.clone(),
    }
}

/// Build the TrieSet for a request (used by both backends' ingestion).
pub fn build_trieset(
    request: &JoinRequest,
    catalog: &BTreeMap<String, RecordBatch>,
) -> Result<TrieSet> {
    let plan = plan::plan(request, catalog)?;
    let rows: Vec<Vec<Vec<Datum>>> = plan
        .relations
        .iter()
        .map(|pr| {
            let batch = &catalog[&pr.name];
            (0..batch.n_rows()).map(|r| batch.row_datums(r)).collect()
        })
        .collect();
    TrieSet::build(plan, &rows)
}

/// Run one verification scenario end to end.
pub fn verify_scenario(
    scenario: impl Into<String>,
    request: &JoinRequest,
    catalog: &BTreeMap<String, RecordBatch>,
) -> Result<Report> {
    let ts = build_trieset(request, catalog)?;

    // Leapfrog, full result (no limit).
    let lf = leapfrog::run(&ts, None, None)?;

    // Oracle: same null filtering/projection, independent join.
    let raw_rows: Vec<Vec<Vec<Datum>>> = ts
        .plan
        .relations
        .iter()
        .map(|pr| {
            let batch = &catalog[&pr.name];
            (0..batch.n_rows()).map(|r| batch.row_datums(r)).collect()
        })
        .collect();
    let projected = naive::project_and_filter(&ts.plan, &raw_rows);
    let nv = naive::enumerate(&ts.plan, &projected);

    let cartesian_intermediate: u64 = projected.iter().map(|r| r.len() as u64).product();
    let trie_nodes = ts.tries.iter().map(|t| t.node_count()).sum::<usize>();

    let mut lf_rendered: Vec<Vec<String>> = lf
        .rows
        .iter()
        .map(|r| r.iter().map(datum_string).collect())
        .collect();
    lf_rendered.sort();
    let mut naive_sorted: Vec<Vec<String>> = nv
        .rows
        .iter()
        .map(|r| r.iter().map(datum_string).collect())
        .collect();
    naive_sorted.sort();

    let output_vars: Vec<String> = ts
        .plan
        .projection
        .iter()
        .map(|&gl| ts.plan.ordered_vars[gl].clone())
        .collect();

    let results_match = lf_rendered == naive_sorted;

    Ok(Report {
        scenario: scenario.into(),
        output_vars,
        leapfrog_rows: lf_rendered,
        naive_rows: naive_sorted,
        results_match,
        leapfrog: LeapfrogCost {
            seeks: lf.stats.seeks,
            nexts: lf.stats.nexts,
            opens: lf.stats.opens,
            ups: lf.stats.ups,
            rows_emitted: lf.stats.rows_emitted,
        },
        naive_combinations: nv.combinations_tested,
        naive_equality_checks: nv.equality_checks,
        cartesian_intermediate,
        trie_nodes,
    })
}

/// Convenience: a join request over (relation, variables) occurrences.
pub fn make_request(rels: Vec<(&str, Vec<&str>)>, null_policy: crate::types::NullPolicy) -> JoinRequest {
    JoinRequest {
        relations: rels
            .into_iter()
            .map(|(r, vs)| crate::plan::RelationOccurrence {
                relation: r.into(),
                variables: vs.into_iter().map(String::from).collect(),
            })
            .collect(),
        null_policy,
        projection: None,
        limit: None,
    }
}
