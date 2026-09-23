//! Memory accounting.
//!
//! The budget explicitly **includes buffers** — it is not just the size of the
//! records held in memory. [`Budget::plan`] reserves fixed costs first (input
//! read buffer, output write buffer, per-run merge buffers) and hands the
//! engine the remaining bytes for resident records. The engine flushes a run
//! whenever resident records approach that allowance.

use crate::error::{Error, Result};
use crate::scanner::READ_BUFFER;

/// Minimum merge fan-in that still reduces run count. A 1-way merge makes no
/// progress (one run in, one run out), so the engine always reserves at least
/// two lanes.
pub const MIN_MERGE_LANES: usize = 2;

/// Hard floor: fixed buffers + two merge lanes + the minimum record allowance.
/// Computed from the parts so it cannot drift from the accounting above.
pub const MIN_BUDGET: u64 = (READ_BUFFER + OUTPUT_BUFFER + SPILL_WORK) as u64
    + (MIN_MERGE_LANES as u64) * (MERGE_LANE_BUFFER + MERGE_LANE_HEAD) as u64
    + MIN_RECORD_ALLOWANCE as u64;

/// Output stream write buffer.
pub const OUTPUT_BUFFER: usize = 16 * 1024;
/// Per-lane readahead buffer while streaming a value.
pub const MERGE_LANE_BUFFER: usize = 8 * 1024;
/// Worst-case resident head prefix per merge lane (one full first chunk).
pub const MERGE_LANE_HEAD: usize = 16 * 1024;
/// Transient work buffers used while streaming one giant record to a run
/// (writer chunk assembly + scanner chunk staging).
pub const SPILL_WORK: usize = 48 * 1024;
/// Minimum bytes left for resident records after fixed reservations.
pub const MIN_RECORD_ALLOWANCE: usize = 4 * 1024;

/// Resolved memory plan for one sort job.
#[derive(Debug, Clone, Copy)]
pub struct Budget {
    /// Total bytes the job is allowed to use across all tracked buffers.
    pub total: u64,
    /// Resident-record allowance (what triggers a run flush).
    pub record_allowance: usize,
    pub read_buffer: usize,
    pub output_buffer: usize,
    pub lane_buffer: usize,
    /// Merge fan-in supported under this budget (extra passes if reduced).
    pub lane_count: usize,
}

impl Budget {
    /// Plan a budget with a target maximum number of simultaneous merge lanes.
    /// Fixed buffers are reserved first; if the budget cannot fund
    /// `max_lanes` lanes plus [`MIN_RECORD_ALLOWANCE`], lanes are reduced (the
    /// merge then performs extra passes) rather than exceeding the budget.
    pub fn plan(total: u64, max_lanes: usize) -> Result<Budget> {
        if total < MIN_BUDGET {
            return Err(Error::BudgetTooSmall {
                budget: total,
                min: MIN_BUDGET,
            });
        }
        // Fixed costs present in every job: input read buffer, output write
        // buffer, and the transient giant-spill work area.
        let base_fixed = (READ_BUFFER + OUTPUT_BUFFER + SPILL_WORK) as u64;
        let per_lane = (MERGE_LANE_BUFFER + MERGE_LANE_HEAD) as u64;
        // Never go below MIN_MERGE_LANES: a 1-way merge does not shrink runs.
        let wanted = max_lanes.clamp(MIN_MERGE_LANES, 1 << 20);
        let mut lanes = wanted as u64;
        loop {
            let lane_bytes = lanes * per_lane;
            let left = total.saturating_sub(base_fixed + lane_bytes);
            if left >= MIN_RECORD_ALLOWANCE as u64 || lanes == MIN_MERGE_LANES as u64 {
                break;
            }
            lanes -= 1;
        }
        let lane_bytes = lanes * per_lane;
        let record_allowance = (total - base_fixed - lane_bytes) as usize;
        Ok(Budget {
            total,
            record_allowance,
            read_buffer: READ_BUFFER,
            output_buffer: OUTPUT_BUFFER,
            lane_buffer: MERGE_LANE_BUFFER,
            lane_count: lanes as usize,
        })
    }

    /// Number of simultaneous merge lanes supported under this plan.
    pub fn lanes(&self) -> usize {
        self.lane_count
    }

    /// Resident bytes permitted for in-memory records.
    pub fn record_bytes(&self) -> usize {
        self.record_allowance
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_tiny_budget() {
        assert!(matches!(
            Budget::plan(1024, 8),
            Err(Error::BudgetTooSmall { .. })
        ));
    }

    #[test]
    fn reserves_buffers_and_scales_lanes() {
        let small = Budget::plan(MIN_BUDGET, 8).unwrap();
        assert!(small.record_allowance >= MIN_RECORD_ALLOWANCE);
        assert!(small.lanes() >= 1);

        let big = Budget::plan(64 * 1024 * 1024, 8).unwrap();
        assert_eq!(big.lanes(), 8);
        let accounted = big.record_allowance
            + big.read_buffer
            + big.output_buffer
            + SPILL_WORK
            + big.lanes() * (big.lane_buffer + MERGE_LANE_HEAD);
        assert_eq!(accounted as u64, big.total);
    }

    #[test]
    fn never_exceeds_total() {
        for &b in &[MIN_BUDGET, 200_000u64, 1_000_000, 16u64 << 20] {
            let plan = Budget::plan(b, 16).unwrap();
            let accounted = plan.record_allowance
                + plan.read_buffer
                + plan.output_buffer
                + SPILL_WORK
                + plan.lanes() * (plan.lane_buffer + MERGE_LANE_HEAD);
            assert_eq!(accounted as u64, b, "budget {b} oversubscribed");
        }
    }
}
