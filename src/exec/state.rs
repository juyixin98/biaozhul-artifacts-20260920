//! Job state machine for join runs.
//!
//! A submitted join becomes a [`JobRecord`] keyed by a unique run id. The
//! status transitions are:
//!
//! ```text
//! Running ──engine ok, cursor──▶ Truncated ──resume──▶ Running ──▶ Complete
//!    │                                                        ▲
//!    └────────── engine error ──────────▶ Failed ─────────────┘ (terminal)
//! ```
//!
//! Illegal transitions (resuming a completed job, fetching an unknown run id,
//! paging a failed job's result) are [`ErrorKind::State`] errors with stable
//! codes, distinct from input, resource and compute failures.

use serde::Serialize;

use crate::error::{Error, Result};
use crate::exec::budget::Budget;
use crate::exec::replay::ReplayEvent;
use crate::operator::iejoin::Cursor;
use crate::operator::plan::{JoinCounters, OutPair};
use crate::operator::predicate::JoinSpec;
use crate::types::batch::Relation;

/// Lifecycle status of one join run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    /// Engine is (re)running. Transient inside this synchronous backend but
    /// part of the contract.
    Running,
    /// All pairs emitted; result is final.
    Complete,
    /// Output budget reached; more pairs are pending behind the cursor.
    Truncated,
    /// Engine failed; `error` on the record carries the classified cause.
    Failed,
}

impl JobStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            JobStatus::Running => "running",
            JobStatus::Complete => "complete",
            JobStatus::Truncated => "truncated",
            JobStatus::Failed => "failed",
        }
    }
}

/// Everything the backend retains about one run: enough to page the result,
/// resume a truncation and replay the decision log.
pub struct JobRecord {
    pub run_id: String,
    pub status: JobStatus,
    pub spec: JoinSpec,
    pub budget: Budget,
    pub left: Relation,
    pub right: Relation,
    /// Counters accumulated over all batches of this run.
    pub counters: JoinCounters,
    /// Pairs accumulated over all batches, in deterministic emission order.
    pub pairs: Vec<OutPair>,
    /// Resume cursor while `status == Truncated`.
    pub cursor: Option<Cursor>,
    /// Classified failure while `status == Failed`.
    pub error: Option<Error>,
    /// In-memory copy of the replay log (also mirrored to disk if configured).
    pub events: Vec<ReplayEvent>,
}

impl JobRecord {
    pub fn new(
        run_id: String,
        spec: JoinSpec,
        budget: Budget,
        left: Relation,
        right: Relation,
    ) -> Self {
        Self {
            run_id,
            status: JobStatus::Running,
            spec,
            budget,
            left,
            right,
            counters: JoinCounters::default(),
            pairs: Vec::new(),
            cursor: None,
            error: None,
            events: Vec::new(),
        }
    }

    /// Guard: the job must be in a state whose accumulated result is
    /// fetchable. Failed jobs re-raise their classified error.
    pub fn require_fetchable(&self) -> Result<()> {
        match self.status {
            JobStatus::Complete | JobStatus::Truncated => Ok(()),
            JobStatus::Running => Err(Error::state(
                "job_not_finished",
                format!("job '{}' is still running", self.run_id),
            )),
            JobStatus::Failed => Err(self
                .error
                .clone()
                .unwrap_or_else(|| Error::compute("job failed without a recorded cause"))),
        }
    }

    /// Guard: only a truncated job can be resumed.
    pub fn require_resumable(&self) -> Result<Cursor> {
        match (self.status, self.cursor) {
            (JobStatus::Truncated, Some(cursor)) => Ok(cursor),
            (JobStatus::Truncated, None) => Err(Error::compute(
                "job is truncated but has no resume cursor",
            )),
            (status, _) => Err(Error::state(
                "invalid_transition",
                format!(
                    "job '{}' cannot be resumed from status '{}'",
                    self.run_id,
                    status.as_str()
                ),
            )),
        }
    }
}
