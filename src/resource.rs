//! Resource budgets, output truncation and controlled batching.
//!
//! The join engines themselves only stream pairs; this layer decides how many
//! pairs are materialised and what happens when a hard limit is reached.
//!
//! # Truncation vs error
//!
//! * The **page limit** is a soft limit: when the result multiset is larger
//!   than the requested page, the page is returned with `complete = false`.
//!   The caller fetches the rest with strictly increasing offsets; every page
//!   is a deterministic slice of the normalized pair order, so pages never
//!   overlap and never skip a pair.
//! * The **row caps**, **candidate-word cap** and **materialized-pair cap**
//!   are hard limits: exceeding them returns
//!   [`crate::error::ErrorCategory::ResourceExhausted`] before any page is
//!   returned, because continuing would violate the configured memory bound.
//!
//! # Why pages are built from a normalized full result
//!
//! The IEJoin engine emits pairs in sweep order (outer-key groups, then p2
//! permutation order), which is *not* a monotone rank of the result. Taking a
//! raw prefix of the emission stream per page would therefore split the
//! normalized multiset incorrectly (later pages could contain pairs that sort
//! before pairs served on earlier pages). To make paging a pure function of
//! `(plan, inputs, offset, limit)`, the full pair multiset is materialized
//! once, normalized to `(left_row asc, right_row asc)` and then sliced. The
//! materialized-pair cap bounds this step deterministically.

use serde::{Deserialize, Serialize};

use crate::batch::{Column, TypedBatch};
use crate::error::{JoinError, Result};
use crate::operator::iejoin;
use crate::operator::metrics::JoinMetrics;
use crate::operator::plan::JoinPlan;
use crate::operator::reference::{nested_loop, SinkReply};
use crate::operator::sort::{Orientation, SortedColumn};

/// Which join implementation executes a page.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Engine {
    /// The IEJoin-style bitmap backend under test.
    Iejoin,
    /// The nested-loop oracle.
    NestedLoop,
}

/// Hard and soft resource budgets for one join workload.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResourceBudget {
    /// Hard cap on total input rows per side.
    pub max_rows_per_side: usize,
    /// Hard cap on 64-bit candidate-bitmap words (one word per 64 right rows).
    pub max_candidate_words: usize,
    /// Hard cap on the number of output pairs materialized (and therefore
    /// normalized) for a page workload.
    pub max_materialized_pairs: usize,
    /// Default maximum pairs materialised when the request gives no limit.
    pub default_output_limit: usize,
    /// Absolute ceiling accepted for a per-request page limit.
    pub max_output_limit: usize,
}

impl Default for ResourceBudget {
    fn default() -> Self {
        ResourceBudget {
            max_rows_per_side: 1_000_000,
            max_candidate_words: 1 << 18, // ~2M slots
            max_materialized_pairs: 1 << 22, // ~4.2M pairs (~32 MiB)
            default_output_limit: 1024,
            max_output_limit: 1 << 20,
        }
    }
}

impl ResourceBudget {
    /// Check the per-side input row caps.
    pub fn check_rows(&self, left: &TypedBatch, right: &TypedBatch) -> Result<()> {
        for (side, rows) in [("left", left.row_count()), ("right", right.row_count())] {
            if rows > self.max_rows_per_side {
                return Err(JoinError::resource(
                    "row_cap_exceeded",
                    format!("{side} side has {rows} rows, cap is {}", self.max_rows_per_side),
                )
                .with("side", side)
                .with("rows", rows)
                .with("cap", self.max_rows_per_side));
            }
        }
        Ok(())
    }

    /// Check the candidate-bitmap word cap for the IEJoin engine. Only
    /// right-side rows matchable on predicate 2 own bitmap slots.
    pub fn check_candidate_words(&self, plan: &JoinPlan, right: &TypedBatch) -> Result<()> {
        let b2_idx = right.column_index(&plan.predicates[1].right_column)?;
        let matchable = SortedColumn::build(right.column(b2_idx)).matchable_count();
        let words = matchable.div_ceil(64);
        if words > self.max_candidate_words {
            return Err(JoinError::resource(
                "candidate_word_cap_exceeded",
                format!(
                    "candidate bitmap needs {words} words for {matchable} matchable right rows, cap is {}",
                    self.max_candidate_words
                ),
            )
            .with("words_required", words)
            .with("cap", self.max_candidate_words)
            .with("matchable_right_rows", matchable));
        }
        Ok(())
    }

    /// Stream the whole result once just to count it, enforcing the hard cap
    /// on materializable output. Returns the exact result size when it fits.
    fn check_result_size(
        &self,
        engine: Engine,
        plan: &JoinPlan,
        left: &TypedBatch,
        right: &TypedBatch,
    ) -> Result<u64> {
        let mut seen: u64 = 0;
        run_streaming(engine, plan, left, right, &mut |_, _| {
            seen += 1;
            if seen as usize > self.max_materialized_pairs {
                SinkReply::Stop
            } else {
                SinkReply::Continue
            }
        })?;
        if seen as usize > self.max_materialized_pairs {
            return Err(JoinError::resource(
                "result_pair_cap_exceeded",
                format!(
                    "result has more than {} pairs (cap); narrow the predicates or raise the cap",
                    self.max_materialized_pairs
                ),
            )
            .with("pairs_observed", seen)
            .with("cap", self.max_materialized_pairs));
        }
        Ok(seen)
    }

    /// Validate a requested page size against the configured ceiling.
    pub fn resolve_limit(&self, requested: Option<usize>) -> Result<usize> {
        let limit = requested.unwrap_or(self.default_output_limit);
        if limit == 0 {
            return Err(JoinError::input("invalid_page_limit", "limit must be > 0"));
        }
        if limit > self.max_output_limit {
            return Err(JoinError::resource(
                "output_limit_too_large",
                format!("requested limit {limit}, cap is {}", self.max_output_limit),
            )
            .with("requested", limit)
            .with("cap", self.max_output_limit));
        }
        Ok(limit)
    }
}

/// A controlled-batch page request: deterministic output slice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PageRequest {
    /// Pairs to skip in normalized output order. Must be zero on the first
    /// page and equal the previous response's `next_offset` afterwards.
    #[serde(default)]
    pub offset: u64,
    /// Maximum pairs materialised on this page.
    pub limit: usize,
}

/// Why a result multiset is (or is not) fully delivered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompletionReason {
    /// This page contains the tail of the multiset.
    Complete,
    /// The page limit was reached; more pairs exist at `next_offset`.
    PageLimit,
}

/// One materialised page of the join result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PageOutcome {
    /// Stable row-identity pairs on this page, normalized to
    /// (`left_row` asc, `right_row` asc) order.
    pub pairs: Vec<(u32, u32)>,
    /// Offset to use for the next page.
    pub next_offset: u64,
    /// False when the page stopped because of its output budget.
    pub complete: bool,
    pub reason: CompletionReason,
    /// Engine counters for the full underlying execution (pages are slices of
    /// one normalized result), so counters stay identical across pages.
    pub metrics: JoinMetrics,
}

fn run_streaming<'a>(
    engine: Engine,
    plan: &'a JoinPlan,
    left: &'a TypedBatch,
    right: &'a TypedBatch,
    sink: &mut dyn FnMut(u32, u32) -> SinkReply,
) -> Result<JoinMetrics> {
    match engine {
        Engine::Iejoin => iejoin::iejoin(plan, left, right, sink),
        Engine::NestedLoop => nested_loop(plan, left, right, sink),
    }
}

/// Execute one deterministic page of a join.
///
/// The fixed input is scanned and the full result is normalized to
/// `(left_row asc, right_row asc)`, then the deterministic slice at
/// `[offset, offset + limit)` is returned. Paging is therefore a pure function
/// of `(plan, left, right, offset, limit)` and the union of chained pages is
/// the complete result multiset without overlap or gaps.
pub fn execute_page(
    engine: Engine,
    plan: &JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
    budget: &ResourceBudget,
    page: PageRequest,
) -> Result<PageOutcome> {
    budget.check_rows(left, right)?;
    if engine == Engine::Iejoin {
        budget.check_candidate_words(plan, right)?;
    }
    let limit = budget.resolve_limit(Some(page.limit))?;

    let total = budget.check_result_size(engine, plan, left, right)?;
    if page.offset > total {
        return Err(JoinError::state(
            "offset_ahead_of_result",
            format!("offset {} is past the end of the result ({total} pairs)", page.offset),
        )
        .with("offset", page.offset)
        .with("result_size", total));
    }

    let mut all: Vec<(u32, u32)> = Vec::with_capacity(total as usize);
    let metrics = run_streaming(engine, plan, left, right, &mut |l, r| {
        all.push((l, r));
        SinkReply::Continue
    })?;
    all.sort_unstable();

    let start = page.offset as usize;
    let end = (start + limit).min(total as usize);
    let pairs: Vec<(u32, u32)> = all[start..end].to_vec();
    let truncated = end < total as usize;
    let collected = pairs.len() as u64;
    let (complete, reason) = if truncated {
        (false, CompletionReason::PageLimit)
    } else {
        (true, CompletionReason::Complete)
    };
    Ok(PageOutcome {
        pairs,
        next_offset: page.offset + collected,
        complete,
        reason,
        metrics,
    })
}

/// Size of the full result multiset without materialising it. A client can
/// compare it against the size of the paginated union.
pub fn result_size(
    engine: Engine,
    plan: &JoinPlan,
    left: &TypedBatch,
    right: &TypedBatch,
    budget: &ResourceBudget,
) -> Result<u64> {
    budget.check_rows(left, right)?;
    if engine == Engine::Iejoin {
        budget.check_candidate_words(plan, right)?;
    }
    budget.check_result_size(engine, plan, left, right)
}

/// Matchable (non-NULL, non-NaN) original row ids of one column, ascending by
/// value. Exposed for diagnostics and replay tooling.
#[must_use]
pub fn matchable_rows_asc(col: &Column) -> Vec<u32> {
    SortedColumn::build(col).rows(Orientation::Asc).to_vec()
}
