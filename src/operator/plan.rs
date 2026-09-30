//! Shared data preparation for the two join operators.
//!
//! A [`Plan`] precomputes, for both predicates, the two sorted permutations
//! and the position map required by IEJoin:
//!
//! * **p1 permutations** order build and scan side by the first key; they
//!   drive the monotone merge scan that inserts build rows into the bitmap;
//! * **p2 permutation** orders the build side by the second key and defines
//!   bitmap rank; `p2_pos[build_row]` is the inverse position map;
//! * the scan side's p2 key is turned into a rank window by binary searching
//!   the build-side p2 permutation — strict vs non-strict predicates use
//!   different boundaries so an equal-value group is either wholly inside or
//!   wholly outside.
//!
//! Sorts are stable on ties with the original row id, which makes
//! permutations deterministic and keeps duplicate rows as distinct
//! identities with their full Cartesian multiplicity.

use serde::{Deserialize, Serialize};

use crate::error::Error;
use crate::operator::predicate::{Comparator, JoinSpec, Predicate};
use crate::types::batch::Batch;

/// One emitted match, identified by original positional row ids.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutPair {
    pub left_row: usize,
    pub right_row: usize,
}

impl OutPair {
    pub fn new(left_row: usize, right_row: usize) -> Self {
        Self { left_row, right_row }
    }
}

/// Deterministic cost counters. The first three metrics use the same
/// semantics for both engines so they are directly comparable:
///
/// * `pairs_considered` — Cartesian pairs examined by the operator
///   (nested loop: `nL*nR`; IE join: merge-comparison probes);
/// * `p1_comparisons` — first-predicate evaluations on an examined pair;
/// * `p2_candidate_accesses` — pairs surviving p1 that reached p2
///   (nested loop: direct evaluations; IE join: total bitmap slots covered by
///   rank windows, including slots currently unset);
/// * `p2_word_probes` — IE join only: 64-bit bitmap words touched;
/// * `output_rows` — emitted matches.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct JoinCounters {
    pub pairs_considered: u64,
    pub p1_comparisons: u64,
    pub p2_candidate_accesses: u64,
    pub p2_word_probes: u64,
    pub output_rows: u64,
}

/// Which physical side supplies the rows inserted into the bitmap.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BuildSide {
    Left,
    Right,
}

/// Normalised direction of p2 after folding away the build-side choice:
/// every case reads as "scan p2 `Cmp` build p2".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScanCmp {
    /// scan < build
    Lt,
    /// scan <= build
    Le,
    /// scan > build
    Gt,
    /// scan >= build
    Ge,
}

/// One active, non-NULL row: local id plus the two keys.
pub type KeyRow = (usize, i64, i64);

/// Normalised, comparison-ready join plan.
pub struct Plan {
    pub build_side: BuildSide,
    /// Build rows indexed by local id (sorted by row id), `(id,p1,p2)`.
    pub build: Vec<KeyRow>,
    /// Scan rows indexed by local id (sorted by row id), `(id,p1,p2)`.
    pub scan: Vec<KeyRow>,
    /// Local build ids ordered by p1 key (stable, id tie-break).
    pub build_p1_order: Vec<usize>,
    /// Local scan ids ordered by p1 key (stable, id tie-break).
    pub scan_p1_order: Vec<usize>,
    /// Local build ids ordered by p2 key (stable, id tie-break) = bitmap rank.
    pub build_p2_order: Vec<usize>,
    /// Inverse p2 position map: local build id -> p2 rank.
    pub p2_pos: Vec<usize>,
    /// Merge scan runs high-to-low (`>`/`>=` on p1).
    pub p1_descending: bool,
    /// p1 admits equal keys (`<=`, `>=`).
    pub p1_inclusive: bool,
    /// Normalised p2 comparator: "scan p2 op build p2".
    pub p2: ScanCmp,
    pub left_rows: usize,
    pub right_rows: usize,
}

impl Plan {
    /// Extracts keys and builds the two permutations plus the position map.
    pub fn prepare(spec: &JoinSpec, left: &Batch, right: &Batch) -> Result<Plan, Error> {
        spec.validate_against(left, right)?;

        // p1 decides the build/scan split: for `<`/`<=` the left side holds
        // the smaller values and its rows are inserted; for `>`/`>=` the
        // right side is inserted instead.
        let build_side = if spec.p1.comparator.is_descending() {
            BuildSide::Right
        } else {
            BuildSide::Left
        };

        let column_values = |batch: &Batch, pred: &Predicate, left_col: bool| -> Vec<Option<i64>> {
            let name = if left_col {
                &pred.left_column
            } else {
                &pred.right_column
            };
            let idx = batch.column_index(name).expect("validated upstream");
            batch.columns[idx].values.clone()
        };

        let l1 = column_values(left, &spec.p1, true);
        let r1 = column_values(right, &spec.p1, false);
        let l2 = column_values(left, &spec.p2, true);
        let r2 = column_values(right, &spec.p2, false);

        let active = |k1: &[Option<i64>], k2: &[Option<i64>]| -> Vec<KeyRow> {
            k1.iter()
                .zip(k2.iter())
                .enumerate()
                .filter_map(|(id, (a, b))| match (a, b) {
                    (Some(x), Some(y)) => Some((id, *x, *y)),
                    _ => None,
                })
                .collect()
        };

        let (mut build, mut scan) = match build_side {
            BuildSide::Left => (active(&l1, &l2), active(&r1, &r2)),
            BuildSide::Right => (active(&r1, &r2), active(&l1, &l2)),
        };

        // Explicit id tie-break: deterministic regardless of sort stability.
        let mut build_p1_order: Vec<usize> = (0..build.len()).collect();
        build_p1_order.sort_by(|&a, &b| build[a].1.cmp(&build[b].1).then(a.cmp(&b)));
        let mut scan_p1_order: Vec<usize> = (0..scan.len()).collect();
        scan_p1_order.sort_by(|&a, &b| scan[a].1.cmp(&scan[b].1).then(a.cmp(&b)));
        let mut build_p2_order: Vec<usize> = (0..build.len()).collect();
        build_p2_order.sort_by(|&a, &b| build[a].2.cmp(&build[b].2).then(a.cmp(&b)));

        let mut p2_pos = vec![0usize; build.len()];
        for (rank, &row) in build_p2_order.iter().enumerate() {
            p2_pos[row] = rank;
        }

        // Normalise p2 to "scan p2 op build p2"; when the build side is the
        // right relation the textual direction (`L op R`) is mirrored.
        let p2 = match (build_side, spec.p2.comparator) {
            (BuildSide::Left, Comparator::Lt) => ScanCmp::Lt,
            (BuildSide::Left, Comparator::Le) => ScanCmp::Le,
            (BuildSide::Left, Comparator::Gt) => ScanCmp::Gt,
            (BuildSide::Left, Comparator::Ge) => ScanCmp::Ge,
            (BuildSide::Right, Comparator::Lt) => ScanCmp::Gt,
            (BuildSide::Right, Comparator::Le) => ScanCmp::Ge,
            (BuildSide::Right, Comparator::Gt) => ScanCmp::Lt,
            (BuildSide::Right, Comparator::Ge) => ScanCmp::Le,
        };

        build.sort_by(|a, b| a.0.cmp(&b.0));
        scan.sort_by(|a, b| a.0.cmp(&b.0));

        Ok(Plan {
            build_side,
            build,
            scan,
            build_p1_order,
            scan_p1_order,
            build_p2_order,
            p2_pos,
            p1_descending: spec.p1.comparator.is_descending(),
            p1_inclusive: spec.p1.comparator.is_inclusive(),
            p2,
            left_rows: l1.len(),
            right_rows: r1.len(),
        })
    }

    /// Map normalised local ids to `(left_row, right_row)`.
    #[inline]
    pub fn emit(&self, build_local: usize, scan_local: usize) -> OutPair {
        let build_row = self.build[build_local].0;
        let scan_row = self.scan[scan_local].0;
        match self.build_side {
            BuildSide::Left => OutPair::new(build_row, scan_row),
            BuildSide::Right => OutPair::new(scan_row, build_row),
        }
    }

    /// Half-open p2 rank window `[lo, hi)` of build rows that satisfy the
    /// normalised p2 comparison with `scan_key`. Equal-value groups are
    /// included or excluded wholesale according to strict/non-strict edges.
    pub fn p2_window(&self, scan_key: i64) -> (usize, usize) {
        let order = &self.build_p2_order;
        let n = order.len();
        let key_at = |rank: usize| self.build[order[rank]].2;

        // First rank whose key >= probe.
        let lower_bound = |probe: i64| -> usize {
            let (mut lo, mut hi) = (0usize, n);
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                if key_at(mid) < probe {
                    lo = mid + 1;
                } else {
                    hi = mid;
                }
            }
            lo
        };
        // First rank whose key > probe.
        let upper_bound = |probe: i64| -> usize {
            let (mut lo, mut hi) = (0usize, n);
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                if key_at(mid) <= probe {
                    lo = mid + 1;
                } else {
                    hi = mid;
                }
            }
            lo
        };

        match self.p2 {
            ScanCmp::Gt => (0, lower_bound(scan_key)), // build < scan
            ScanCmp::Ge => (0, upper_bound(scan_key)), // build <= scan
            ScanCmp::Le => (lower_bound(scan_key), n), // build >= scan
            ScanCmp::Lt => (upper_bound(scan_key), n), // build > scan
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_keep_equal_groups_together() {
        // build p2 keys, deliberately with a duplicated equal group.
        let plan = Plan {
            build_side: BuildSide::Left,
            build: vec![
                (0, 0, 10),
                (1, 0, 20),
                (2, 0, 20),
                (3, 0, 30),
            ],
            scan: vec![],
            build_p1_order: vec![0, 1, 2, 3],
            scan_p1_order: vec![],
            build_p2_order: vec![0, 1, 2, 3],
            p2_pos: vec![0, 1, 2, 3],
            p1_descending: false,
            p1_inclusive: true,
            p2: ScanCmp::Gt,
            left_rows: 4,
            right_rows: 0,
        };
        // scan > build, probe 20: strict excludes the whole "20" group.
        assert_eq!(plan.p2_window(20), (0, 1));
        // scan >= build at probe 20: non-strict includes the whole group.
        let plan2 = Plan { p2: ScanCmp::Ge, ..clone_plan(&plan) };
        assert_eq!(plan2.p2_window(20), (0, 3));
        // scan < build at probe 20: strict keeps only the "30" group.
        let plan3 = Plan { p2: ScanCmp::Lt, ..clone_plan(&plan) };
        assert_eq!(plan3.p2_window(20), (3, 4));
        // scan <= build at probe 20: keeps the "20" group and above.
        let plan4 = Plan { p2: ScanCmp::Le, ..clone_plan(&plan) };
        assert_eq!(plan4.p2_window(20), (1, 4));
    }

    fn clone_plan(plan: &Plan) -> Plan {
        Plan {
            build_side: plan.build_side,
            build: plan.build.clone(),
            scan: plan.scan.clone(),
            build_p1_order: plan.build_p1_order.clone(),
            scan_p1_order: plan.scan_p1_order.clone(),
            build_p2_order: plan.build_p2_order.clone(),
            p2_pos: plan.p2_pos.clone(),
            p1_descending: plan.p1_descending,
            p1_inclusive: plan.p1_inclusive,
            p2: plan.p2,
            left_rows: plan.left_rows,
            right_rows: plan.right_rows,
        }
    }
}
