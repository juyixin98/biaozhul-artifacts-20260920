//! Shared helpers for integration tests. These construct data through the
//! PUBLIC API only (arrow2 typed batches, indexes, deletions) — the reference
//! answers are hand-computed literals or the independent scalar oracle, never
//! values derived from the bitmap engine under test.

#![allow(dead_code)]

use std::collections::HashSet;
use std::sync::Arc;

use tritmap::batch::{BatchBuilder, TypedBatch};
use tritmap::dataset::Dataset;
use tritmap::query::Expr;
use tritmap::trit::Trit;

/// The 10-row hand-designed NULL-crossing batch used in fixed assertions.
///
/// ```text
/// row  age   city   vip
///  0    10    NY     true
///  1   NULL   LA     false
///  2    30   NULL    true
///  3    10    NY     NULL
///  4   NULL  NULL     NULL   <- fully-NULL row
///  5    30    LA     true
///  6    42    SF     false
///  7   NULL   NY     true
///  8     5   LA     false
///  9    42   NULL    NULL
/// ```
pub fn crossing_batch() -> TypedBatch {
    BatchBuilder::new()
        .add_integer(
            "age",
            vec![
                Some(10),
                None,
                Some(30),
                Some(10),
                None,
                Some(30),
                Some(42),
                None,
                Some(5),
                Some(42),
            ],
        )
        .add_text(
            "city",
            vec![
                Some("NY"),
                Some("LA"),
                None,
                Some("NY"),
                None,
                Some("LA"),
                Some("SF"),
                Some("NY"),
                Some("LA"),
                None,
            ],
        )
        .add_boolean(
            "vip",
            vec![
                Some(true),
                Some(false),
                Some(true),
                None,
                None,
                Some(true),
                Some(false),
                Some(true),
                Some(false),
                None,
            ],
        )
        .build()
        .unwrap()
}

pub fn dataset(batch: TypedBatch, data_version: u64) -> Dataset {
    Dataset::load("t", batch, data_version, &[]).unwrap()
}

/// Apply deletions at successive versions; returns the set of dead rows.
pub fn delete_rows(d: &mut Dataset, data_version: u64, rows: &[usize]) -> HashSet<usize> {
    let mut v = data_version;
    let mut dead = HashSet::new();
    for r in rows {
        v += 1;
        d.delete_row(*r, v).unwrap();
        dead.insert(*r);
    }
    dead
}

/// Hand-transcribed per-row expectations with deleted rows as `None`.
pub fn expected_vec(items: &[(usize, Option<Trit>)], n: usize) -> Vec<Option<Trit>> {
    let mut v: Vec<Option<Trit>> = (0..n).map(|_| None).collect();
    for (row, val) in items {
        v[*row] = *val;
    }
    v
}

pub fn eval_oracle(d: &Dataset, expr: &Expr, dead: &HashSet<usize>) -> Vec<Option<Trit>> {
    tritmap::oracle::oracle_eval(d.batch(), expr, dead).unwrap()
}

pub fn test_state() -> Arc<tritmap::server::AppState> {
    Arc::new(tritmap::server::AppState::new(
        tritmap::config::Config::default(),
    ))
}
