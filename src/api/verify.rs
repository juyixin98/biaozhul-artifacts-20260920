//! Equivalence verification.
//!
//! The service runs BOTH strategies on every request and compares:
//!
//! * on success — every appended datum for every outer row;
//! * on failure — the error category and code and the outer row at which the
//!   failure occurred.
//!
//! Any disagreement is an [`ErrorCategory::EquivalenceMismatch`]: the service
//! fails closed rather than presenting an uncertain result.
//!
//! The naïve strategy is the reference oracle; the decorrelated pipeline is
//! the system under test. Both are exercised from the same validated plan.

use crate::exec::{ExecError, StrategyOutput};
use crate::query::{ErrorCategory, QueryError};

pub struct EquivalenceReport {
    pub equivalent: bool,
    pub discrepancies: Vec<String>,
}

/// Compare the two strategies' outcomes.
pub fn compare(naive: &StrategyOutput, decorrelated: &StrategyOutput) -> EquivalenceReport {
    let mut discrepancies = Vec::new();

    match (&naive.error, &decorrelated.error) {
        (Some(a), Some(b)) => compare_errors(a, b, &mut discrepancies),
        (None, None) => compare_values(naive, decorrelated, &mut discrepancies),
        (a, b) => discrepancies.push(format!(
            "outcome differs: naive {} but decorrelated {}",
            a.as_ref().map(|e| format!("ERROR[{}]", e.code)).unwrap_or_else(|| "OK".to_string()),
            b.as_ref().map(|e| format!("ERROR[{}]", e.code)).unwrap_or_else(|| "OK".to_string()),
        )),
    }

    EquivalenceReport { equivalent: discrepancies.is_empty(), discrepancies }
}

fn compare_errors(a: &ExecError, b: &ExecError, out: &mut Vec<String>) {
    if a.category != b.category {
        out.push(format!("error category differs: {} vs {}", a.category, b.category));
    }
    if a.code != b.code {
        out.push(format!("error code differs: {} vs {}", a.code, b.code));
    }
    if a.at_outer_row != b.at_outer_row {
        out.push(format!(
            "error outer row differs: {:?} vs {:?}",
            a.at_outer_row, b.at_outer_row
        ));
    }
}

fn compare_values(
    naive: &StrategyOutput,
    decorrelated: &StrategyOutput,
    out: &mut Vec<String>,
) {
    if naive.appended.len() != decorrelated.appended.len() {
        out.push(format!(
            "appended column count differs: {} vs {}",
            naive.appended.len(),
            decorrelated.appended.len()
        ));
        return;
    }
    for (i, (a, b)) in naive.appended.iter().zip(&decorrelated.appended).enumerate() {
        if a.field != b.field {
            out.push(format!("column #{} field differs: {:?} vs {:?}", i, a.field, b.field));
        }
        if a.values.len() != b.values.len() {
            out.push(format!(
                "column `{}` row count differs: {} vs {}",
                a.field.name,
                a.values.len(),
                b.values.len()
            ));
            continue;
        }
        for (row, (x, y)) in a.values.iter().zip(b.values.iter()).enumerate() {
            if x != y {
                out.push(format!(
                    "column `{}` row #{} differs: {} vs {}",
                    a.field.name,
                    row,
                    datum_label(x),
                    datum_label(y)
                ));
            }
        }
    }
}

fn datum_label(d: &crate::batch::Datum) -> String {
    match &d.value {
        None => "NULL".to_string(),
        Some(crate::batch::Scalar::Str(s)) => format!("{:?}", s),
        Some(crate::batch::Scalar::Val(v)) => format!("{:?}", v),
    }
}

/// Turn a non-equivalent report into a fail-closed query error.
pub fn mismatch_error(report: &EquivalenceReport) -> QueryError {
    QueryError::new(
        ErrorCategory::EquivalenceMismatch,
        "strategies_disagreed",
        format!(
            "naive reference and decorrelated pipeline disagreed ({} discrepancy/ies); result withheld",
            report.discrepancies.len()
        ),
        "verify:equivalence",
    )
    .with_detail(serde_json::json!({ "discrepancies": report.discrepancies }))
}
