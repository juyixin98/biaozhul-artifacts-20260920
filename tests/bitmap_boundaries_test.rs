//! Boundary-focused tests:
//! * non-machine-word universe lengths (tail-byte correctness),
//! * `NOT` must not be a word-wise complement,
//! * versioned deletions remove rows from the whole universe and combined
//!   indexes over incompatible domains are rejected,
//! * an all-UNKNOWN partition survives NOT/AND/OR correctly.

mod common;

use std::collections::HashSet;

use common::{crossing_batch, dataset, delete_rows, eval_oracle};
use tritmap::bitset::Bitmap;
use tritmap::error::TritmapError;
use tritmap::partition::{PartitionError, TritPartition};
use tritmap::query::{evaluate, parse_predicate};
use tritmap::trit::Trit;

#[test]
fn tail_bits_are_zero_for_every_awkward_length() {
    for len in [0usize, 1, 7, 8, 9, 63, 64, 65, 67, 127, 128, 129, 200, 1000] {
        let all = Bitmap::all(len);
        all.debug_assert_valid();
        assert_eq!(all.count_ones(), len, "len={len}");

        let c = all.complement();
        c.debug_assert_valid();
        assert_eq!(c.count_ones(), 0, "complement(U) must be empty, len={len}");

        // Set only the last valid bit and complement: exactly len-1 ones and
        // no garbage beyond the universe.
        if len > 0 {
            let mut one = Bitmap::new(len);
            one.set(len - 1);
            let rest = one.complement();
            rest.debug_assert_valid();
            assert_eq!(rest.count_ones(), len - 1, "len={len}");
            assert!(!rest.get(len - 1));
        }
    }
}

#[test]
fn not_does_not_wordwise_negate_on_non_word_universe() {
    // n = 67 spans two u64 words with 63 invalid tail bits in the second.
    let n = 67;
    let d = dataset(build_int_batch(n), 1);
    let expr = parse_predicate("x = 1").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();

    // Every row must be classified exactly once over 67 positions.
    assert_eq!(
        out.true_count() + out.false_count() + out.unknown_count(),
        n
    );
    // Raw last word must never have bits 67..128 set after NOT.
    let not = out.partition.not();
    let words = not.true_set().words();
    if let Some(last) = words.last() {
        let valid_bits = n % 64;
        let invalid_mask = u64::MAX << valid_bits;
        assert_eq!(*last & invalid_mask, 0, "NOT set invalid tail bits");
    }
    not.validate().unwrap();
}

/// Build an n-row INTEGER column "x": alternating 0/1 with every 7th NULL.
fn build_int_batch(n: usize) -> tritmap::batch::TypedBatch {
    use tritmap::batch::BatchBuilder;
    let vals: Vec<Option<i64>> = (0..n)
        .map(|i| if i % 7 == 6 { None } else { Some((i % 2) as i64) })
        .collect();
    BatchBuilder::new().add_integer("x", vals).build().unwrap()
}

#[test]
fn versioned_deletions_exclude_rows_from_all_three_sets() {
    let mut d = dataset(crossing_batch(), 1); // 10 rows
    // Delete a NULL row (1), a TRUE-for-age=10 row (3), and a FALSE row (5).
    let dead = delete_rows(&mut d, 1, &[1, 3, 5]);

    let expr = parse_predicate("age = 10 OR city = 'LA'").unwrap();
    let out = evaluate(&d, &expr, None).unwrap();
    assert_eq!(out.version, 4);
    assert_eq!(out.live, 7);
    assert_eq!(out.deleted_count(), 3);

    // Deleted rows appear in NONE of TRUE/FALSE/UNKNOWN — not as UNKNOWN.
    for r in [1, 3, 5] {
        assert_eq!(out.partition.get(r), None, "row {r} must be outside universe");
        for t in Trit::ALL {
            assert!(!out.partition.set(t).get(r), "row {r} leaked into {t:?}");
        }
    }
    // Live rows are still exactly-one-of-three; counts exclude deletions.
    assert_eq!(
        out.true_count() + out.false_count() + out.unknown_count(),
        out.live
    );
    out.partition.validate().unwrap();

    // Oracle treats the same dead set identically.
    assert_eq!(out.partition.to_row_values(), eval_oracle(&d, &expr, &dead));
}

#[test]
fn historical_versions_keep_deleted_rows_visible() {
    let mut d = dataset(crossing_batch(), 1);
    let _ = delete_rows(&mut d, 1, &[0]);
    let expr = parse_predicate("age = 10").unwrap();

    // At v1 (data snapshot, before the v2 tombstone) row 0 is still live.
    let v1 = evaluate(&d, &expr, Some(1)).unwrap();
    assert_eq!(v1.selected_rows(), vec![0, 3]);
    let v2 = evaluate(&d, &expr, Some(2)).unwrap();
    assert_eq!(v2.selected_rows(), vec![3]);
    let current = evaluate(&d, &expr, None).unwrap();
    assert_eq!(current.selected_rows(), vec![3]);
}

#[test]
fn deletion_rules_are_typed_errors() {
    let mut d = dataset(crossing_batch(), 1);
    assert!(matches!(
        d.delete_row(99, 2),
        Err(TritmapError::RowNotFound { .. })
    ));
    d.delete_row(0, 2).unwrap();
    assert!(matches!(
        d.delete_row(1, 2),
        Err(TritmapError::VersionOutOfOrder { .. })
    ));
    assert!(matches!(
        d.delete_row(0, 3),
        Err(TritmapError::RowAlreadyDeleted { .. })
    ));
}

#[test]
fn incompatible_domains_cannot_be_combined() {
    // Two partitions on identical n but different live domains: AND/OR must
    // fail with DomainMismatch rather than silently mixing sets.
    let n = 10;
    let p1 = TritPartition::constant_on(&Bitmap::from_indices(n, [0, 1, 2]), Trit::True);
    let p2 = TritPartition::constant_on(&Bitmap::from_indices(n, [3, 4, 5]), Trit::False);
    assert!(matches!(
        p1.and(&p2),
        Err(PartitionError::DomainMismatch { .. })
    ));
    assert!(matches!(
        p1.or(&p2),
        Err(PartitionError::DomainMismatch { .. })
    ));
}

#[test]
fn all_unknown_universe_stays_unknown_under_not_and_ops() {
    // 67 rows where x is NULL for every row.
    let b = build_all_null(67);
    let d = dataset(b, 1);

    let leaf = parse_predicate("x = 1").unwrap();
    let out = evaluate(&d, &leaf, None).unwrap();
    assert_eq!(out.unknown_count(), 67);
    assert_eq!(out.true_count(), 0);
    assert_eq!(out.false_count(), 0);

    // NOT over all-UNKNOWN must remain all-UNKNOWN.
    let not = parse_predicate("NOT x = 1").unwrap();
    let out = evaluate(&d, &not, None).unwrap();
    assert_eq!(out.unknown_count(), 67);
    assert_eq!(out.true_count(), 0);

    // UNKNOWN AND FALSE(from a disjoint constant shape) — here combine with a
    // predicate that is FALSE everywhere: x = 1 AND x <> 1 on all NULLs is
    // UNKNOWN AND UNKNOWN = UNKNOWN; use IS NOT NULL to force FALSE rows.
    let e = parse_predicate("x = 1 AND x IS NOT NULL").unwrap();
    let out = evaluate(&d, &e, None).unwrap();
    // UNKNOWN AND FALSE = FALSE on every row: the decisive Kleene row.
    assert_eq!(out.false_count(), 67);
    assert_eq!(out.unknown_count(), 0);
    out.partition.validate().unwrap();

    // Oracle agreement including deletion of one NULL row.
    let mut d2 = dataset(build_all_null(67), 1);
    let dead = delete_rows(&mut d2, 1, &[0, 63, 66]);
    let out = evaluate(&d2, &leaf, None).unwrap();
    assert_eq!(out.unknown_count(), 64, "deleted NULL rows are not UNKNOWN");
    assert_eq!(
        out.partition.to_row_values(),
        eval_oracle(&d2, &leaf, &dead)
    );
}

fn build_all_null(n: usize) -> tritmap::batch::TypedBatch {
    use tritmap::batch::BatchBuilder;
    BatchBuilder::new()
        .add_integer("x", vec![None; n])
        .build()
        .unwrap()
}

#[test]
fn oracle_agrees_under_mixed_deletions_and_nulls() {
    let mut d = dataset(crossing_batch(), 3);
    let dead: HashSet<usize> = delete_rows(&mut d, 3, &[2, 4, 7, 9]);
    for src in [
        "NOT age = 10 AND city = 'NY'",
        "(age > 5 OR vip = TRUE) AND NOT city IS NULL",
        "age IS NULL OR vip IS NOT NULL",
    ] {
        let expr = parse_predicate(src).unwrap();
        let out = evaluate(&d, &expr, None).unwrap();
        out.partition.validate().unwrap();
        assert_eq!(
            out.partition.to_row_values(),
            eval_oracle(&d, &expr, &dead),
            "expr: {src}"
        );
        // Every live row classified, deleted rows never.
        assert_eq!(
            out.true_count() + out.false_count() + out.unknown_count(),
            out.live
        );
    }
}
