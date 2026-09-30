//! Execution metrics common to both join engines.
//!
//! All counters are deterministic for a given input, which makes them usable
//! as test oracles (the high-selectivity test asserts concrete access
//! counts, not just "the function ran").

use serde::{Deserialize, Serialize};

/// Deterministic counters describing work performed by a join execution.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct JoinMetrics {
    /// Driving left rows counted by this execution.
    ///
    /// * Nested loop: every input left row is scanned (NULL rows included).
    /// * IEJoin: only rows matchable on both left keys enter the sweep.
    pub left_rows_scanned: u64,
    /// Rows inserted into the candidate bitmap (matchable right-side p1 rows
    /// only; each such row is inserted at most once).
    pub p1_right_rows_inserted: u64,
    /// Number of 64-bit candidate-bitmap words inspected while scanning
    /// prefixes/suffixes for surviving candidates.
    pub p2_words_scanned: u64,
    /// Number of candidate bit slots covered by the word scans: the sum of
    /// every surviving-prefix length (oriented `right p2` positions probed
    /// against predicate 2). This is the IEJoin analogue of "candidate
    /// evaluations".
    pub p2_probe_positions: u64,
    /// Pair predicate tests performed by the nested-loop reference engine
    /// (`left_rows * right_rows`, including NULL/NaN comparisons). `0` for
    /// IEJoin runs.
    pub nl_pair_evaluations: u64,
    /// Emitted `(left_row, right_row)` pairs (before output truncation).
    pub pairs_emitted: u64,
}

impl JoinMetrics {
    #[must_use]
    pub fn snapshot(&self) -> Self {
        self.clone()
    }
}
