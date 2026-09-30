//! Verification entry point: differential testing of the bitmap engine
//! against the independent scalar [`crate::oracle`].
//!
//! Every check logs a **run identity** (`run_id`, seed), the pinned data and
//! query versions, progress, and the concrete judgment for each discrepancy.
//! Discrepancies are classified into [`FailureCategory`] values instead of a
//! pass/fail boolean, so a failing run says *what* was wrong:
//!
//! * [`FailureCategory::DeletedRowClassified`] — a deleted row appeared in
//!   TRUE/FALSE/UNKNOWN (versioned deletion leaked through an operator),
//! * [`FailureCategory::LiveRowUnclassified`] — a live row belonged to none of
//!   the three sets (or two sets — partition invalidity),
//! * [`FailureCategory::TruthMismatch`] — engine trit != scalar oracle trit,
//! * [`FailureCategory::EnginePartitionInvalid`] — the engine result failed
//!   disjoint/exhaustive self-validation,
//! * [`FailureCategory::CountMismatch`] — aggregate T/F/U/dead counts disagree.

pub mod gen;

use std::collections::HashSet;

use serde::Serialize;
use tracing::{debug, info, warn};

use crate::dataset::Dataset;
use crate::error::Result;
use crate::query::Expr;
use crate::trit::Trit;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureCategory {
    DeletedRowClassified,
    LiveRowUnclassified,
    TruthMismatch,
    EnginePartitionInvalid,
    CountMismatch,
}

impl FailureCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            FailureCategory::DeletedRowClassified => "DELETED_ROW_CLASSIFIED",
            FailureCategory::LiveRowUnclassified => "LIVE_ROW_UNCLASSIFIED",
            FailureCategory::TruthMismatch => "TRUTH_MISMATCH",
            FailureCategory::EnginePartitionInvalid => "ENGINE_PARTITION_INVALID",
            FailureCategory::CountMismatch => "COUNT_MISMATCH",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct RowMismatch {
    pub row: usize,
    pub expected: Option<String>,
    pub actual: Option<String>,
    pub category: FailureCategory,
    pub basis: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    pub run_id: String,
    pub case_name: String,
    pub seed: u64,
    pub data_version: u64,
    pub query_version: u64,
    pub row_space: usize,
    pub live_rows: usize,
    pub checked_rows: usize,
    pub passed: bool,
    pub mismatches: Vec<RowMismatch>,
    pub engine_counts: Counts,
    pub oracle_counts: Counts,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct Counts {
    pub true_rows: usize,
    pub false_rows: usize,
    pub unknown_rows: usize,
    pub deleted_rows: usize,
}

fn count_trits(rows: &[Option<Trit>]) -> Counts {
    let mut c = Counts {
        true_rows: 0,
        false_rows: 0,
        unknown_rows: 0,
        deleted_rows: 0,
    };
    for r in rows {
        match r {
            Some(Trit::True) => c.true_rows += 1,
            Some(Trit::False) => c.false_rows += 1,
            Some(Trit::Unknown) => c.unknown_rows += 1,
            None => c.deleted_rows += 1,
        }
    }
    c
}

/// Run one differential cross-check.
///
/// * `dead_rows` must equal the rows absent from the dataset domain at
///   `version`; it is passed explicitly so the caller (fixtures/generator)
///   asserts its own deletion intent rather than deriving it from the engine.
pub fn cross_check(
    dataset: &Dataset,
    expr: &Expr,
    version: Option<u64>,
    dead_rows: &HashSet<usize>,
    run_id: &str,
    case_name: &str,
    seed: u64,
) -> Result<VerifyReport> {
    let outcome = crate::query::evaluate(dataset, expr, version)?;
    let qv = outcome.version;
    let n = outcome.n;

    info!(
        run_id = %run_id,
        case = %case_name,
        seed,
        data_version = dataset.data_version(),
        query_version = qv,
        row_space = n,
        live = outcome.live,
        expr = ?expr,
        "verify: evaluating expression (versions pinned)"
    );

    // 1. Engine self-validation (disjoint + exhaustive over domain).
    let mut mismatches = Vec::new();
    if let Err(e) = outcome.partition.validate() {
        warn!(run_id = %run_id, error = %e, "verify: engine partition self-validation FAILED");
        mismatches.push(RowMismatch {
            row: usize::MAX,
            category: FailureCategory::EnginePartitionInvalid,
            expected: None,
            actual: None,
            basis: format!("partition.validate(): {e}"),
        });
    }

    // 2. Independent scalar oracle.
    let expected = crate::oracle::oracle_eval(dataset.batch(), expr, dead_rows)?;
    let actual = outcome.partition.to_row_values();

    let oracle_counts = count_trits(&expected);
    let engine_counts = Counts {
        true_rows: outcome.true_count(),
        false_rows: outcome.false_count(),
        unknown_rows: outcome.unknown_count(),
        deleted_rows: outcome.deleted_count(),
    };

    info!(
        run_id = %run_id,
        ?engine_counts,
        ?oracle_counts,
        "verify: comparing engine vs independent scalar oracle"
    );

    // 3. Row-by-row classification. Each row must be exactly one of
    //    TRUE/FALSE/UNKNOWN when live, and none when deleted.
    for row in 0..n {
        if row > 0 && row % 256 == 0 {
            debug!(run_id = %run_id, progress = row, total = n, "verify: progress");
        }
        let exp = expected[row];
        let act = actual[row];
        let intended_dead = dead_rows.contains(&row);

        let category = match (exp, act, intended_dead) {
            (e, a, _) if e == a => continue,
            (None, Some(_), _) => FailureCategory::DeletedRowClassified,
            (Some(_), None, _) => FailureCategory::LiveRowUnclassified,
            _ => FailureCategory::TruthMismatch,
        };
        let basis = match category {
            FailureCategory::DeletedRowClassified => format!(
                "row {row} has a tombstone at/<= v{qv} but engine placed it in {:?}",
                act
            ),
            FailureCategory::LiveRowUnclassified => format!(
                "row {row} is live at v{qv} and oracle says {:?}, but it is absent from T/F/U",
                exp
            ),
            FailureCategory::TruthMismatch => format!(
                "row {row}: scalar Kleene evaluation gives {:?}, bitmap engine gives {:?}",
                exp, act
            ),
            other => format!("{other:?}"),
        };
        warn!(run_id = %run_id, row, ?exp, ?act, category = category.as_str(), "verify: MISMATCH");
        mismatches.push(RowMismatch {
            row,
            expected: exp.map(|t| t.as_str().to_string()),
            actual: act.map(|t| t.as_str().to_string()),
            category,
            basis,
        });
    }

    // 4. Aggregate counts.
    if engine_counts != oracle_counts {
        warn!(run_id = %run_id, ?engine_counts, ?oracle_counts, "verify: COUNT_MISMATCH");
        mismatches.push(RowMismatch {
            row: usize::MAX,
            category: FailureCategory::CountMismatch,
            expected: Some(format!("{oracle_counts:?}")),
            actual: Some(format!("{engine_counts:?}")),
            basis: "aggregate TRUE/FALSE/UNKNOWN/deleted counts differ".into(),
        });
    }

    let passed = mismatches.is_empty();
    if passed {
        info!(
            run_id = %run_id,
            checked = n,
            "verify: PASS — every row is exactly one of TRUE/FALSE/UNKNOWN, deleted rows excluded"
        );
    } else {
        warn!(run_id = %run_id, n_mismatches = mismatches.len(), "verify: FAIL");
    }

    Ok(VerifyReport {
        run_id: run_id.to_string(),
        case_name: case_name.to_string(),
        seed,
        data_version: dataset.data_version(),
        query_version: qv,
        row_space: n,
        live_rows: outcome.live,
        checked_rows: n,
        passed,
        mismatches,
        engine_counts,
        oracle_counts,
    })
}
