//! IEJoin-style two-predicate join engine.
//!
//! The engine consumes a [`Plan`] (sorted permutations + p2 position map) and
//! executes the classic sort-merge/bitmap loop:
//!
//! 1. Scan rows are visited in p1 key order, grouped by equal key.
//! 2. On entering a new key group, the merge pointer inserts **every** build
//!    row admitted by p1 into the candidate bitmap at its p2 rank — strictly
//!    (`build < scan`) for strict p1, inclusively (`build <= scan`) for
//!    non-strict p1. Because the whole equal-key scan group is entered only
//!    after the insertion pointer has passed the boundary, no equal-value
//!    group can be missed by the bitmap update order.
//! 3. Each scan row then probes a half-open p2 rank window
//!    ([`Plan::p2_window`]) of the bitmap; set ranks are emitted as matches.
//!
//! ## Truncation and resumption
//!
//! When `budget.max_output_rows` is reached the engine stops mid-scan and
//! returns a [`Cursor`]: `(scan_pos, inserted, skip_in_row)`. The bitmap
//! state is a pure function of `inserted`, so a resumed run deterministically
//! re-materialises it. Counters are attributed so that the sum over batches
//! equals a single uninterrupted run:
//!
//! * merge comparisons are charged as they happen (re-insertion on resume is
//!   replayed silently);
//! * a scan row's window cost (`p2_candidate_accesses` and `p2_word_probes`)
//!   is charged by the batch that first enters the row.

use serde::{Deserialize, Serialize};

use crate::error::Result;
use crate::exec::budget::{Accounting, Budget};
use crate::operator::bitset::Bitmap;
use crate::operator::plan::{JoinCounters, OutPair, Plan};

/// Resume cursor for a truncated run. See module docs for the invariants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cursor {
    /// Index into `plan.scan_p1_order` of the next scan row to process.
    pub scan_pos: usize,
    /// Prefix length of `plan.build_p1_order` already inserted in the bitmap.
    pub inserted: usize,
    /// Matches of the scan row at `scan_pos` already emitted (skip on resume).
    pub skip_in_row: u64,
}

/// Output of one engine batch.
#[derive(Debug, Clone, Default)]
pub struct Output {
    pub pairs: Vec<OutPair>,
    pub counters: JoinCounters,
    /// `Some(cursor)` when the run stopped early on the output budget.
    pub cursor: Option<Cursor>,
}

impl Output {
    pub fn is_complete(&self) -> bool {
        self.cursor.is_none()
    }
}

/// Execute the join described by `plan`. `resume` continues a truncated run.
pub fn run(plan: &Plan, budget: &Budget, resume: Option<Cursor>) -> Result<Output> {
    let accounting = Accounting::new();
    let mut counters = JoinCounters::default();
    let mut pairs: Vec<OutPair> = Vec::new();

    let start = resume.unwrap_or(Cursor {
        scan_pos: 0,
        inserted: 0,
        skip_in_row: 0,
    });

    let mut bitmap = Bitmap::new(plan.build.len());
    // Replay the deterministic bitmap state; intentionally uncharged so that
    // summed counters equal a single uninterrupted run.
    for &build_id in plan.build_p1_order.iter().take(start.inserted) {
        bitmap.set(plan.p2_pos[build_id]);
    }
    let mut inserted = start.inserted;

    let scan_order = &plan.scan_p1_order;
    let mut pos = start.scan_pos;
    // On resume the boundary for the first scan row's key is already covered
    // by `inserted`; skip re-advancing for that key.
    let mut last_key: Option<i64> = if start.inserted > 0 || start.skip_in_row > 0 {
        scan_order
            .get(pos)
            .map(|&id| plan.scan[id].1)
    } else {
        None
    };
    let mut cursor = None;

    'scan: while pos < scan_order.len() {
        let scan_id = scan_order[pos];
        let scan_key = plan.scan[scan_id].1;

        if last_key != Some(scan_key) {
            // Enter a new equal-key group: insert every build row admitted by
            // p1 BEFORE any row of the group probes the bitmap.
            while inserted < plan.build_p1_order.len() {
                let build_id = plan.build_p1_order[inserted];
                let build_key = plan.build[build_id].1;
                counters.pairs_considered += 1;
                counters.p1_comparisons += 1;
                accounting.charge_p1(1, budget)?;
                let admitted = if plan.p1_inclusive {
                    build_key <= scan_key
                } else {
                    build_key < scan_key
                };
                if !admitted {
                    break;
                }
                bitmap.set(plan.p2_pos[build_id]);
                inserted += 1;
            }
            last_key = Some(scan_key);
        }

        let (lo, hi) = plan.p2_window(plan.scan[scan_id].2);
        // Window cost is charged by the batch that first enters the row.
        if start.skip_in_row == 0 || pos != start.scan_pos {
            let slots = (hi - lo) as u64;
            counters.p2_candidate_accesses += slots;
            counters.p2_word_probes += Bitmap::word_probes(lo, hi);
            accounting.charge_p2(slots, budget)?;
        }

        let mut skip = if pos == start.scan_pos {
            start.skip_in_row
        } else {
            0
        };
        let mut emitted_this_row: u64 = 0;
        let mut truncated = false;
        bitmap.for_each_set_in_while(lo, hi, |rank| {
            if skip > 0 {
                skip -= 1;
                return true;
            }
            if let Some(cap) = budget.max_output_rows {
                if pairs.len() as u64 >= cap {
                    // Refusing to emit a real pair: the run is truncated.
                    truncated = true;
                    return false;
                }
            }
            let build_id = plan.build_p2_order[rank];
            pairs.push(plan.emit(build_id, scan_id));
            counters.output_rows += 1;
            emitted_this_row += 1;
            true
        });
        if truncated {
            // Resume re-enters this row, skipping what was already emitted
            // (plus any pre-existing skip from an earlier batch).
            let prior = if pos == start.scan_pos {
                start.skip_in_row
            } else {
                0
            };
            cursor = Some(Cursor {
                scan_pos: pos,
                inserted,
                skip_in_row: prior + emitted_this_row,
            });
            break 'scan;
        }
        pos += 1;
    }

    Ok(Output {
        pairs,
        counters,
        cursor,
    })
}
