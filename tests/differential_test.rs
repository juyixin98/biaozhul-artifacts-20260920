//! Differential tests: hundreds of randomized synthetic cases cross-checked
//! against the independent scalar oracle. The oracle never uses the bitmap
//! core, and every discrepancy is asserted with its concrete
//! [`FailureCategory`] plus the regenerating seed.

mod common;

use std::collections::HashSet;
use std::sync::Arc;

use common::test_state;
use tritmap::dataset::Dataset;
use tritmap::query::parse_predicate;
use tritmap::verify::gen::{generate_case, BOUNDARY_LENGTHS};
use tritmap::verify::{cross_check, FailureCategory};

fn run_case(seed: u64, rows: usize, density: u32) -> tritmap::verify::VerifyReport {
    let case = generate_case(format!("diff-n{rows}"), seed, rows, density).unwrap();
    let expr = parse_predicate(&case.expr_src).unwrap();
    let mut dataset = Dataset::load("verify", case.batch, 1, &[]).unwrap();

    let mut dead = HashSet::new();
    let mut v = 1u64;
    for row in &case.dead_rows {
        v += 1;
        dataset.delete_row(*row, v).unwrap();
        dead.insert(*row);
    }

    cross_check(
        &dataset,
        &expr,
        None,
        &dead,
        &format!("test-seed-{seed}"),
        &case.name,
        seed,
    )
    .unwrap()
}

#[test]
fn every_boundary_length_passes_differential_check() {
    // Non-machine-word lengths incl. 1, 7, 9, 63, 65, 67, 127, 129, 200.
    for (i, rows) in BOUNDARY_LENGTHS.iter().enumerate() {
        let seed = 1000 + i as u64;
        let report = run_case(seed, *rows, 45);
        assert!(
            report.passed,
            "n={rows} seed={seed} mismatches={:?}",
            report.mismatches
        );
        assert_eq!(report.checked_rows, *rows);
        assert_eq!(
            report.engine_counts.true_rows
                + report.engine_counts.false_rows
                + report.engine_counts.unknown_rows,
            report.live_rows
        );
    }
}

#[test]
fn all_null_columns_produce_all_unknown_and_pass() {
    let report = run_case(0xABC, 67, 100);
    assert!(report.passed, "{:?}", report.mismatches);
    // No row is selected, every live row is UNKNOWN.
    assert_eq!(report.engine_counts.true_rows, 0);
    assert_eq!(
        report.engine_counts.unknown_rows,
        report.live_rows,
        "all live rows must be UNKNOWN"
    );
}

#[test]
fn randomized_cases_agree_with_scalar_oracle() {
    // Fixed seed set: reproducible, spans small and larger row spaces.
    for seed in 1..=60u64 {
        let rows = 1 + (seed as usize * 37) % 257; // varied, crosses word edges
        let density = (seed * 13) % 100;
        let report = run_case(seed.wrapping_mul(0x9E37), rows, density as u32);
        assert!(
            report.passed,
            "seed={seed} rows={rows} density={density}: {:#?}",
            report.mismatches
        );
    }
}

#[test]
fn state_driven_verification_reports_pass_and_run_identity() {
    let state: Arc<tritmap::server::AppState> = test_state();
    let reports = state
        .run_differential_verify(777, BOUNDARY_LENGTHS.len() + 5, 193, 40)
        .unwrap();
    assert_eq!(reports.len(), BOUNDARY_LENGTHS.len() + 5);
    for r in &reports {
        assert!(r.passed, "{:?}", r.mismatches);
        assert!(r.run_id.starts_with("run-"), "run_id must correlate logs: {}", r.run_id);
    }
    // The dedicated all-UNKNOWN case sits right after the boundary sweep.
    let all_null = &reports[BOUNDARY_LENGTHS.len()];
    assert_eq!(all_null.engine_counts.true_rows, 0);
    assert_eq!(all_null.engine_counts.unknown_rows, all_null.live_rows);
}

#[test]
fn failure_categories_are_distinguishable() {
    // Sanity: categories serialize to distinct stable codes.
    let cats = [
        FailureCategory::DeletedRowClassified,
        FailureCategory::LiveRowUnclassified,
        FailureCategory::TruthMismatch,
        FailureCategory::EnginePartitionInvalid,
        FailureCategory::CountMismatch,
    ];
    let codes: Vec<_> = cats.iter().map(|c| c.as_str()).collect();
    for w in codes.windows(2) {
        assert_ne!(w[0], w[1]);
    }
    // Bad parameters are errors, not silent clamping to success.
    let state = test_state();
    assert!(state.run_differential_verify(1, 0, 10, 10).is_err());
    assert!(state.run_differential_verify(1, 2, 10, 101).is_err());
}
