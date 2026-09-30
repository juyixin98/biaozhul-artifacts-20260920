//! Nested-loop reference operator.
//!
//! This is the deliberately naive ground-truth operator: it examines the full
//! Cartesian product and evaluates both predicates on every pair with the
//! shared [`Comparator`] semantics (NULL never matches). It exists so tests
//! and the verification entry can cross-check the IEJoin engine against an
//! implementation that shares no plan, bitmap or merge logic with it.
//!
//! Cost counters use the same metric definitions as
//! [`crate::operator::iejoin`] so the two are directly comparable:
//!
//! * `pairs_considered` — every Cartesian pair examined (`nL * nR`,
//!   including rows whose keys are NULL);
//! * `p1_comparisons` — p1 evaluations (one per examined pair);
//! * `p2_candidate_accesses` — pairs that survived p1 and reached p2;
//! * `p2_word_probes` — always 0 (no bitmap here);
//! * `output_rows` — emitted matches.

use serde::{Deserialize, Serialize};

use crate::error::Result;
use crate::exec::budget::{Accounting, Budget};
use crate::operator::plan::{JoinCounters, OutPair};
use crate::operator::predicate::JoinSpec;
use crate::types::batch::Batch;

/// Resume cursor for a truncated nested-loop run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct NlCursor {
    /// Next left row to examine.
    pub left: usize,
    /// Next right row to examine for `left`.
    pub right: usize,
}

/// Output of one nested-loop batch.
#[derive(Debug, Clone, Default)]
pub struct NlOutput {
    pub pairs: Vec<OutPair>,
    pub counters: JoinCounters,
    /// `Some(cursor)` when the run stopped early on the output budget.
    pub cursor: Option<NlCursor>,
}

/// Run the nested-loop join. `resume` continues a previously truncated run.
pub fn run(
    spec: &JoinSpec,
    left: &Batch,
    right: &Batch,
    budget: &Budget,
    resume: Option<NlCursor>,
) -> Result<NlOutput> {
    spec.validate_against(left, right)?;
    let l1 = &left.column(&spec.p1.left_column)?.values;
    let l2 = &left.column(&spec.p2.left_column)?.values;
    let r1 = &right.column(&spec.p1.right_column)?.values;
    let r2 = &right.column(&spec.p2.right_column)?.values;

    let accounting = Accounting::new();
    let mut counters = JoinCounters::default();
    let mut pairs = Vec::new();
    let start = resume.unwrap_or(NlCursor { left: 0, right: 0 });
    let mut cursor = None;

    'outer: for i in start.left..l1.len() {
        let j0 = if i == start.left { start.right } else { 0 };
        for j in j0..r1.len() {
            counters.pairs_considered += 1;
            counters.p1_comparisons += 1;
            accounting.charge_p1(1, budget)?;
            if !spec.p1.comparator.eval(l1[i], r1[j]) {
                continue;
            }
            counters.p2_candidate_accesses += 1;
            accounting.charge_p2(1, budget)?;
            if !spec.p2.comparator.eval(l2[i], r2[j]) {
                continue;
            }
            if let Some(cap) = budget.max_output_rows {
                if pairs.len() as u64 >= cap {
                    cursor = Some(NlCursor {
                        left: i,
                        right: j,
                    });
                    break 'outer;
                }
            }
            pairs.push(OutPair::new(i, j));
            counters.output_rows += 1;
        }
    }

    Ok(NlOutput {
        pairs,
        counters,
        cursor,
    })
}
