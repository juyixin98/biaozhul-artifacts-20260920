//! Fixed, hand-computed assertions for SQL three-valued filtering over the
//! NULL-crossing fixture. Expected rows are transcribed from scalar Kleene
//! logic on paper (see `common::crossing_batch`), not produced by the engine.

mod common;

use std::collections::HashSet;

use common::{crossing_batch, dataset, eval_oracle, expected_vec};
use tritmap::query::{evaluate, parse_predicate};
use tritmap::trit::Trit;

fn sorted(v: Vec<usize>) -> Vec<usize> {
    let mut v = v;
    v.sort();
    v
}

const N: usize = 10;

#[test]
fn equality_splits_true_false_unknown() {
    let d = dataset(crossing_batch(), 1);
    let expr = parse_predicate("age = 10").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();

    assert_eq!(sorted(out.selected_rows()), vec![0, 3]);
    assert_eq!(
        sorted(out.partition.set(Trit::False).iter_ones().collect()),
        vec![2, 5, 6, 8, 9]
    );
    assert_eq!(
        sorted(out.partition.set(Trit::Unknown).iter_ones().collect()),
        vec![1, 4, 7]
    );
    out.partition.validate().unwrap();

    // Independent oracle agreement.
    let oracle = eval_oracle(&d, &expr, &HashSet::new());
    assert_eq!(out.partition.to_row_values(), oracle);
    // Counts partition the whole universe exactly once.
    assert_eq!(out.true_count() + out.false_count() + out.unknown_count(), N);
}

#[test]
fn and_with_null_crossings() {
    // age = 10 AND city = 'NY'  (hand-computed per row)
    let d = dataset(crossing_batch(), 1);
    let expr = parse_predicate("age = 10 AND city = 'NY'").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();

    let want = expected_vec(
        &[
            (0, Some(Trit::True)),
            (1, Some(Trit::False)), // UNKNOWN AND FALSE = FALSE
            (2, Some(Trit::False)), // FALSE AND UNKNOWN = FALSE
            (3, Some(Trit::True)),
            (4, Some(Trit::Unknown)), // UNKNOWN AND UNKNOWN
            (5, Some(Trit::False)),
            (6, Some(Trit::False)),
            (7, Some(Trit::Unknown)), // UNKNOWN AND TRUE = UNKNOWN
            (8, Some(Trit::False)),
            (9, Some(Trit::False)), // FALSE AND UNKNOWN
        ],
        N,
    );
    assert_eq!(out.partition.to_row_values(), want);
    assert_eq!(sorted(out.selected_rows()), vec![0, 3]);
    assert_eq!(out.partition.to_row_values(), eval_oracle(&d, &expr, &HashSet::new()));
}

#[test]
fn or_with_null_crossings() {
    // age = 10 OR city = 'NY'
    let d = dataset(crossing_batch(), 1);
    let expr = parse_predicate("age = 10 OR city = 'NY'").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();

    let want = expected_vec(
        &[
            (0, Some(Trit::True)),
            (1, Some(Trit::Unknown)), // UNKNOWN OR FALSE
            (2, Some(Trit::Unknown)), // FALSE OR UNKNOWN
            (3, Some(Trit::True)),
            (4, Some(Trit::Unknown)),
            (5, Some(Trit::False)),
            (6, Some(Trit::False)),
            (7, Some(Trit::True)), // UNKNOWN OR TRUE = TRUE
            (8, Some(Trit::False)),
            (9, Some(Trit::Unknown)),
        ],
        N,
    );
    assert_eq!(out.partition.to_row_values(), want);
    assert_eq!(sorted(out.selected_rows()), vec![0, 3, 7]);
    assert_eq!(out.partition.to_row_values(), eval_oracle(&d, &expr, &HashSet::new()));
}

#[test]
fn not_is_truth_falsity_swap_unknown_fixed() {
    // NOT (age = 10): UNKNOWN rows must stay UNKNOWN — a word-wise !TRUE
    // bitmap would instead turn them into FALSE and also set tail bits.
    let d = dataset(crossing_batch(), 1);
    let expr = parse_predicate("NOT age = 10").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();

    assert_eq!(
        sorted(out.partition.true_set().iter_ones().collect()),
        vec![2, 5, 6, 8, 9]
    );
    assert_eq!(sorted(out.selected_rows()), vec![2, 5, 6, 8, 9]);
    assert_eq!(
        sorted(out.partition.set(Trit::False).iter_ones().collect()),
        vec![0, 3]
    );
    assert_eq!(
        sorted(out.partition.set(Trit::Unknown).iter_ones().collect()),
        vec![1, 4, 7]
    );
    // Tail cleanliness on a non-machine-word universe.
    out.partition.true_set().debug_assert_valid();
    out.partition.set(Trit::Unknown).debug_assert_valid();
    assert_eq!(out.partition.to_row_values(), eval_oracle(&d, &expr, &HashSet::new()));
}

#[test]
fn is_null_is_data_not_unknown_and_composes() {
    let d = dataset(crossing_batch(), 1);

    let is_null = parse_predicate("age IS NULL").unwrap();
    let out = evaluate(&d, &is_null, None).unwrap();
    assert_eq!(sorted(out.selected_rows()), vec![1, 4, 7]);
    assert_eq!(out.unknown_count(), 0, "IS NULL must never yield UNKNOWN");

    // age IS NOT NULL AND age = 10  -> only the two non-null 10s are TRUE;
    // NULL rows are FALSE (because IS NOT NULL is FALSE for them), never UNKNOWN.
    let e = parse_predicate("age IS NOT NULL AND age = 10").unwrap();
    let out = evaluate(&d, &e, None).unwrap();
    assert_eq!(sorted(out.selected_rows()), vec![0, 3]);
    assert_eq!(out.unknown_count(), 0);
    assert_eq!(out.false_count(), 8);
}

#[test]
fn nested_boolean_preserves_three_way_partition() {
    // NOT (a = 10 AND b = 'NY') OR vip = FALSE — every row exactly one trit.
    let d = dataset(crossing_batch(), 1);
    let expr = parse_predicate(
        "NOT (age = 10 AND city = 'NY') OR vip = FALSE",
    )
    .unwrap();
    let out = evaluate(&d, &expr, None).unwrap();
    out.partition.validate().unwrap();
    assert_eq!(
        out.true_count() + out.false_count() + out.unknown_count(),
        N
    );
    assert_eq!(out.partition.to_row_values(), eval_oracle(&d, &expr, &HashSet::new()));
}
