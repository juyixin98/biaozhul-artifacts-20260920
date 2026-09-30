//! IEJoin-style batch range join with two inequality predicates.
//!
//! # Algorithm sketch
//!
//! Given predicates `p1: A1 OP1 B1` and `p2: A2 OP2 B2` (left columns `A`,
//! right columns `B`):
//!
//! 1. Every key column is turned into a [`SortedColumn`]: **ascending and
//!    descending permutations** plus inverse position maps. NULL/NaN rows are
//!    absent from the permutations (`position = -1`).
//! 2. Outer (left) rows are walked in `A1` descending order for `<` / `<=`
//!    (ascending for `>` / `>=`), so the qualifying right-side set always
//!    *grows*. Growth happens in the matching `B1` orientation.
//! 3. Qualifying right rows are inserted into a candidate bitmap indexed by
//!    their position in the `B2` permutation, whose orientation is chosen so
//!    that predicate-2 survivors always form a **prefix**.
//! 4. For each outer row the prefix bits are enumerated; set bits are output
//!    pairs.
//!
//! # Equal-value groups
//!
//! Strict and non-strict predicates differ at the equal-value boundary
//! ([`SortedColumn::lower_bound`] vs [`SortedColumn::upper_bound`]). Outer
//! rows sharing an `A1` value are processed as one group: the `B1` cursor
//! moves to the exact boundary **once**, *before* any row of the group is
//! probed. This group-atomic update prevents an equal group from being
//! half-missed or double counted.

use serde::{Deserialize, Serialize};

use crate::batch::{try_cmp_cells, Cell, TypedBatch};
use crate::error::{JoinError, Result};
use crate::operator::bitset::BitSet;
use crate::operator::metrics::JoinMetrics;
use crate::operator::plan::{CmpOp, JoinPlan};
use crate::operator::reference::{PairSink, SinkReply};
use crate::operator::sort::{Orientation, SortedColumn};

/// Qualifying prefix length of a sorted side relative to probe key `v`.
///
/// The sweep permutation is chosen so that qualifying rows always form a
/// prefix; strict vs non-strict boundaries come from
/// [`SortedColumn::upper_bound`] / [`SortedColumn::lower_bound`].
fn prefix_len(sorted: &SortedColumn<'_>, op: CmpOp, v: Cell<'_>) -> Result<usize> {
    let m = sorted.matchable_count();
    let len = match op {
        CmpOp::Lt => m - sorted.upper_bound(v)?,
        CmpOp::Le => m - sorted.lower_bound(v)?,
        CmpOp::Gt => sorted.lower_bound(v)?,
        CmpOp::Ge => sorted.upper_bound(v)?,
    };
    Ok(len)
}

fn orientation_for(op: CmpOp) -> Orientation {
    if op.is_ascending() {
        Orientation::Desc
    } else {
        Orientation::Asc
    }
}

/// Preprocessed, replayable state for one join plan over fixed batches.
pub(crate) struct PreparedJoin<'a> {
    plan: &'a JoinPlan,
    a1: SortedColumn<'a>,
    a2: SortedColumn<'a>,
    b1: SortedColumn<'a>,
    b2: SortedColumn<'a>,
}

impl<'a> PreparedJoin<'a> {
    pub(crate) fn build(
        plan: &'a JoinPlan,
        left: &'a TypedBatch,
        right: &'a TypedBatch,
    ) -> Result<Self> {
        let a1_idx = left.column_index(&plan.predicates[0].left_column)?;
        let a2_idx = left.column_index(&plan.predicates[1].left_column)?;
        let b1_idx = right.column_index(&plan.predicates[0].right_column)?;
        let b2_idx = right.column_index(&plan.predicates[1].right_column)?;
        // Normally `JoinPlan::validate` proves dtype agreement. When a plan is
        // built directly (e.g. from a corrupt replay), fail here as a dynamic
        // compute error instead of panicking deep inside a binary search.
        for (i, (li, ri)) in [(a1_idx, b1_idx), (a2_idx, b2_idx)].iter().enumerate() {
            if left.column(*li).dtype != right.column(*ri).dtype {
                return Err(JoinError::compute(
                    "incomparable_cells",
                    format!(
                        "predicate {i} compares {} with {}",
                        left.column(*li).dtype.as_str(),
                        right.column(*ri).dtype.as_str()
                    ),
                ));
            }
        }
        Ok(PreparedJoin {
            plan,
            a1: SortedColumn::build(left.column(a1_idx)),
            a2: SortedColumn::build(left.column(a2_idx)),
            b1: SortedColumn::build(right.column(b1_idx)),
            b2: SortedColumn::build(right.column(b2_idx)),
        })
    }

    /// Outer rows matchable on **both** left keys, in the p1 sweep order.
    fn outer_rows(&self) -> Vec<u32> {
        let orient1 = orientation_for(self.plan.predicates[0].op);
        self.a1
            .rows(orient1)
            .iter()
            .copied()
            .filter(|&r| self.a2.position(r as usize, Orientation::Asc).is_some())
            .collect()
    }

    /// Run the IEJoin sweep, streaming pairs to `sink`.
    pub(crate) fn run(&self, sink: PairSink<'_>) -> Result<JoinMetrics> {
        let op1 = self.plan.predicates[0].op;
        let op2 = self.plan.predicates[1].op;
        let orient1 = orientation_for(op1);
        let orient2 = orientation_for(op2);

        let outer = self.outer_rows();
        let b1_rows = self.b1.rows(orient1);
        let b2_rows = self.b2.rows(orient2);

        let mut bitmap = BitSet::zeros(self.b2.matchable_count());
        let mut cursor = 0usize;
        let mut metrics = JoinMetrics::default();

        let mut gi = 0usize;
        while gi < outer.len() {
            // One equal-A1 value group (group-atomic p1 update).
            let group_key = self.a1.col_key(outer[gi]);
            let mut gj = gi + 1;
            while gj < outer.len()
                && try_cmp_cells(group_key, self.a1.col_key(outer[gj]))
                    .ok_or_else(|| {
                        JoinError::compute(
                            "incomparable_cells",
                            "outer group key changed dtype family mid-sweep",
                        )
                    })?
                    .is_eq()
            {
                gj += 1;
            }

            metrics.left_rows_scanned += (gj - gi) as u64;

            // Move the B1 insertion cursor to the strict/non-strict boundary
            // for the whole group before probing any of its members.
            let target = prefix_len(&self.b1, op1, group_key)?;
            while cursor < target {
                let r = b1_rows[cursor] as usize;
                // A row non-matchable on B2 has no bit in any B2 permutation.
                if let Some(pos) = self.b2.position(r, orient2) {
                    if !bitmap.test(pos) {
                        bitmap.set(pos);
                        metrics.p1_right_rows_inserted += 1;
                    }
                }
                cursor += 1;
            }

            // Probe every outer row of the group: p2 survivors are exactly
            // the set bits inside the oriented prefix.
            for &lrow in &outer[gi..gj] {
                let key2 = self.a2.col_key(lrow);
                let k = prefix_len(&self.b2, op2, key2)?;
                if k == 0 {
                    continue;
                }
                metrics.p2_probe_positions += k as u64;
                metrics.p2_words_scanned += k.div_ceil(64) as u64;

                let mut emit = |pos: usize, metrics: &mut JoinMetrics| -> SinkReply {
                    let rrow = b2_rows[pos];
                    metrics.pairs_emitted += 1;
                    sink(lrow, rrow)
                };
                if enumerate_prefix(&bitmap, k, &mut emit, &mut metrics) == SinkReply::Stop {
                    return Ok(metrics);
                }
            }

            gi = gj;
        }
        Ok(metrics)
    }
}

fn enumerate_prefix(
    bitmap: &BitSet,
    k: usize,
    emit: &mut dyn FnMut(usize, &mut JoinMetrics) -> SinkReply,
    metrics: &mut JoinMetrics,
) -> SinkReply {
    let mut stopped = false;
    let mut visit = |wi: usize, mask: u64, stop: &mut bool| {
        let mut w = mask;
        while w != 0 {
            let bit = w.trailing_zeros() as usize;
            w &= w - 1;
            if emit(wi * 64 + bit, metrics) == SinkReply::Stop {
                *stop = true;
                return;
            }
        }
    };
    let full_words = k / 64;
    for wi in 0..full_words {
        visit(wi, bitmap.word(wi), &mut stopped);
        if stopped {
            return SinkReply::Stop;
        }
    }
    let rem = k % 64;
    if rem != 0 {
        visit(full_words, bitmap.word(full_words) & ((1u64 << rem) - 1), &mut stopped);
        if stopped {
            return SinkReply::Stop;
        }
    }
    SinkReply::Continue
}

// ---------------------------------------------------------------------------
// Explainable intermediate state (replay logs / debugging)
// ---------------------------------------------------------------------------

/// One key's sorted permutations, inverse maps and equal-group boundaries.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct KeyState {
    pub side: String,
    pub column: String,
    pub row_count: usize,
    /// Original row ids that cannot match (SQL NULL or float NaN).
    pub nonmatchable_rows: Vec<u32>,
    pub asc_rows: Vec<u32>,
    pub desc_rows: Vec<u32>,
    /// Position in `asc_rows` per original row; `-1` = non-matchable.
    pub asc_pos: Vec<i64>,
    /// Position in `desc_rows` per original row; `-1` = non-matchable.
    pub desc_pos: Vec<i64>,
    /// Ascending equal-value groups as `[start, end)` pairs.
    pub groups: Vec<[usize; 2]>,
}

/// Replayable snapshot of all preprocessed state plus live engine metrics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JoinExplain {
    pub run_id: String,
    pub predicates: Vec<String>,
    pub keys: Vec<KeyState>,
    pub metrics: JoinMetrics,
    pub result_size: u64,
}

fn key_state(side: &str, column: &str, sorted: &SortedColumn<'_>) -> KeyState {
    let row_count = sorted.total_len();
    let nonmatchable_rows = (0..row_count)
        .filter(|&r| sorted.position(r, Orientation::Asc).is_none())
        .map(|r| r as u32)
        .collect();
    KeyState {
        side: side.to_owned(),
        column: column.to_owned(),
        row_count,
        nonmatchable_rows,
        asc_rows: sorted.rows(Orientation::Asc).to_vec(),
        desc_rows: sorted.rows(Orientation::Desc).to_vec(),
        asc_pos: sorted.positions(Orientation::Asc).to_vec(),
        desc_pos: sorted.positions(Orientation::Desc).to_vec(),
        groups: sorted
            .groups(Orientation::Asc)
            .into_iter()
            .map(|(lo, hi, _)| [lo, hi])
            .collect(),
    }
}

/// Build the explain snapshot: permutations, maps, groups and live metrics.
pub fn explain(
    run_id: &str,
    plan: &JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
) -> Result<JoinExplain> {
    let prepared = PreparedJoin::build(plan, left, right)?;
    let metrics = prepared.run(&mut |_, _| SinkReply::Continue)?;
    let result_size = metrics.pairs_emitted;
    let predicates = plan
        .predicates
        .iter()
        .map(|p| format!("left.{} {} right.{}", p.left_column, p.op.as_str(), p.right_column))
        .collect();
    let keys = vec![
        key_state("left", &plan.predicates[0].left_column, &prepared.a1),
        key_state("left", &plan.predicates[1].left_column, &prepared.a2),
        key_state("right", &plan.predicates[0].right_column, &prepared.b1),
        key_state("right", &plan.predicates[1].right_column, &prepared.b2),
    ];
    Ok(JoinExplain { run_id: run_id.to_owned(), predicates, keys, metrics, result_size })
}

/// Run a validated plan with the IEJoin engine.
pub fn iejoin(
    plan: &JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
    sink: PairSink<'_>,
) -> Result<JoinMetrics> {
    let prepared = PreparedJoin::build(plan, left, right)?;
    prepared.run(sink)
}
