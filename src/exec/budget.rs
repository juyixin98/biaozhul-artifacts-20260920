//! Explicit resource budgets for a join run.
//!
//! Every limit is opt-in (`None` = unlimited). Exceeding one is a
//! distinguished [`ErrorKind::Resource`] failure with a stable code, never a
//! panic and never a silent drop: callers either get a complete, ordered
//! result page plus a resume cursor, or a classified resource error.

use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};

/// Resource limits applied to one join execution.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Budget {
    /// Maximum output pairs retained for the run.
    pub max_output_rows: Option<u64>,
    /// Maximum first-predicate comparisons (cost guard).
    pub max_p1_comparisons: Option<u64>,
    /// Maximum p2 candidate/bitmap-slot accesses.
    pub max_p2_accesses: Option<u64>,
    /// Maximum number of output rows returned in one page.
    pub page_size: Option<u64>,
}

impl Budget {
    pub fn unlimited() -> Self {
        Self::default()
    }

    pub fn with_page_size(mut self, page_size: u64) -> Self {
        self.page_size = Some(page_size);
        self
    }
}

/// Mutable accounting handle shared by an engine run.
#[derive(Debug, Default)]
pub struct Accounting {
    p1: AtomicU64,
    p2: AtomicU64,
}

impl Accounting {
    pub fn new() -> Self {
        Self::default()
    }

    #[inline]
    pub fn charge_p1(&self, n: u64, budget: &Budget) -> Result<()> {
        let v = self.p1.fetch_add(n, Ordering::Relaxed) + n;
        if let Some(limit) = budget.max_p1_comparisons {
            if v > limit {
                return Err(Error::resource(
                    "p1_comparison_budget_exceeded",
                    format!("p1 comparison budget {limit} exceeded (reached {v})"),
                ));
            }
        }
        Ok(())
    }

    #[inline]
    pub fn charge_p2(&self, n: u64, budget: &Budget) -> Result<()> {
        let v = self.p2.fetch_add(n, Ordering::Relaxed) + n;
        if let Some(limit) = budget.max_p2_accesses {
            if v > limit {
                return Err(Error::resource(
                    "p2_access_budget_exceeded",
                    format!("p2 candidate access budget {limit} exceeded (reached {v})"),
                ));
            }
        }
        Ok(())
    }

    pub fn p1(&self) -> u64 {
        self.p1.load(Ordering::Relaxed)
    }

    pub fn p2(&self) -> u64 {
        self.p2.load(Ordering::Relaxed)
    }
}
