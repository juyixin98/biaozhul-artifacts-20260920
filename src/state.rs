//! Stateful run registry: appended inputs, fixed plans and paging cursors.
//!
//! A [`Run`] owns the accumulated left/right [`TypedBatch`] inputs and one
//! fixed [`JoinPlan`]. Inputs may be appended until the run is *sealed* by the
//! first join page; afterwards appends are rejected as state conflicts so an
//! in-flight cursor can never silently point at a different multiset.
//!
//! Registry operations are synchronous and in-memory. Every error carries a
//! stable code and replay-safe coordinates.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::batch::TypedBatch;
use crate::error::{JoinError, Result};
use crate::operator::plan::{JoinPlan, JoinPredicate};
use crate::resource::{
    execute_page, Engine, PageOutcome, PageRequest, ResourceBudget,
};

/// Which side an append targets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Left,
    Right,
}

/// One registered join workload.
pub struct Run {
    pub id: String,
    pub plan: JoinPlan,
    pub left: TypedBatch,
    pub right: TypedBatch,
    /// Becomes true after the first page; further appends are rejected.
    sealed: bool,
    /// Last cursor returned to a client; the next page must continue from it.
    last_offset: Option<u64>,
    /// Total pages served (monotonic, part of the replay coordinate).
    pages_served: u64,
}

impl Run {
    /// True after the first page was produced.
    #[must_use]
    pub fn is_sealed(&self) -> bool {
        self.sealed
    }

    /// Append a typed batch to one side before sealing.
    pub fn append(&mut self, side: Side, batch: TypedBatch) -> Result<()> {
        if self.sealed {
            return Err(JoinError::state(
                "run_sealed",
                "inputs cannot change after the first join page; start a new run",
            )
            .with("run_id", self.id.clone()));
        }
        match side {
            Side::Left => self.left.concat_in_place(&batch),
            Side::Right => self.right.concat_in_place(&batch),
        }
    }

    /// Produce the next deterministic page.
    pub fn page(
        &mut self,
        engine: Engine,
        budget: &ResourceBudget,
        request: PageRequest,
    ) -> Result<PageOutcome> {
        // Cursor discipline: pages must chain monotonically from zero.
        match self.last_offset {
            None if request.offset != 0 => {
                return Err(JoinError::state(
                    "cursor_must_start_at_zero",
                    format!("first page requires offset 0, got {}", request.offset),
                )
                .with("run_id", self.id.clone())
                .with("offset", request.offset));
            }
            Some(expected) if request.offset != expected => {
                return Err(JoinError::state(
                    "stale_or_over_ahead_cursor",
                    format!(
                        "cursor conflict: page expects offset {expected}, request sent {}",
                        request.offset
                    ),
                )
                .with("run_id", self.id.clone())
                .with("expected_offset", expected)
                .with("requested_offset", request.offset));
            }
            _ => {}
        }
        let outcome = execute_page(engine, &self.plan, &self.left, &self.right, budget, request)?;
        self.sealed = true;
        self.last_offset = Some(outcome.next_offset);
        self.pages_served += 1;
        Ok(outcome)
    }
}

/// Serializable status view of a run (used by diagnostics).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RunStatus {
    pub run_id: String,
    pub predicates: Vec<String>,
    pub left_rows: usize,
    pub right_rows: usize,
    pub sealed: bool,
    pub last_offset: Option<u64>,
    pub pages_served: u64,
}

/// In-memory, thread-safe registry of runs.
#[derive(Default)]
pub struct RunRegistry {
    runs: Mutex<HashMap<String, Run>>,
    seq: AtomicU64,
}

impl RunRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Create a run with a client-prefixed, server-sequenced id.
    pub fn create(
        &self,
        id_prefix: &str,
        predicates: [JoinPredicate; 2],
        left: TypedBatch,
        right: TypedBatch,
    ) -> Result<String> {
        // Validation happens at creation time, before any state is stored.
        let plan = JoinPlan::validate(predicates, &left, &right)?;
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let id = format!("{id_prefix}-{seq:08}");
        let run = Run {
            id: id.clone(),
            plan,
            left,
            right,
            sealed: false,
            last_offset: None,
            pages_served: 0,
        };
        let mut runs = self.runs.lock().expect("registry mutex poisoned");
        if runs.contains_key(&id) {
            return Err(JoinError::state("run_id_collision", id));
        }
        runs.insert(id.clone(), run);
        Ok(id)
    }

    fn with_run<T>(&self, id: &str, f: impl FnOnce(&mut Run) -> Result<T>) -> Result<T> {
        let mut runs = self.runs.lock().expect("registry mutex poisoned");
        let run = runs.get_mut(id).ok_or_else(|| {
            JoinError::state("unknown_run", format!("no run with id {id:?}"))
                .with("run_id", id)
        })?;
        f(run)
    }

    /// Append inputs to an existing run.
    pub fn append(&self, id: &str, side: Side, batch: TypedBatch) -> Result<()> {
        self.with_run(id, |run| run.append(side, batch))
    }

    /// Serve the next page from an existing run.
    pub fn page(
        &self,
        id: &str,
        engine: Engine,
        budget: &ResourceBudget,
        request: PageRequest,
    ) -> Result<PageOutcome> {
        self.with_run(id, |run| run.page(engine, budget, request))
    }

    /// Read a status snapshot.
    pub fn status(&self, id: &str) -> Result<RunStatus> {
        self.with_run(id, |run| {
            Ok(RunStatus {
                run_id: run.id.clone(),
                predicates: run
                    .plan
                    .predicates
                    .iter()
                    .map(|p| {
                        format!("left.{} {} right.{}", p.left_column, p.op.as_str(), p.right_column)
                    })
                    .collect(),
                left_rows: run.left.row_count(),
                right_rows: run.right.row_count(),
                sealed: run.is_sealed(),
                last_offset: run.last_offset,
                pages_served: run.pages_served,
            })
        })
    }

    /// Access a validated run plan/inputs for an explain snapshot.
    pub fn with_explain<T>(
        &self,
        id: &str,
        f: impl FnOnce(&JoinPlan, &TypedBatch, &TypedBatch) -> Result<T>,
    ) -> Result<T> {
        self.with_run(id, |run| f(&run.plan, &run.left, &run.right))
    }
}
