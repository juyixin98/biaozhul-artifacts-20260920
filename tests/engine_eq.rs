//! IEJoin vs. the independent oracles.
//!
//! Required question coverage (task statement):
//! * all four inequality directions against a nested-loop reference;
//! * literal expected multisets for the directions (not "it just runs");
//! * candidate-access counters asserted as concrete numbers;
//! * all-equal values, empty sides, high-selectivity data;
//! * the reference answers are never produced by the engine under test:
//!   literals are hand-derived, and the random checks use a separate
//!   brute-force routine in `common`.

mod common;

use common::*;
use iejoin_backend::fixtures::RangeFixture;
use iejoin_backend::operator::plan::CmpOp;

#[test]
fn four_directions_match_literals_oracle_and_each_other_on_mixed_data() {
    let (left, right) = mixed().unwrap();

    // Hand-derived expected answer multisets for the `mixed()` fixture.
    let expected = [
        (
            CmpOp::Lt,
            CmpOp::Lt,
            vec![(0u32, 1u32)],
        ),
        (
            CmpOp::Le,
            CmpOp::Le,
            vec![(0, 0), (0, 1), (0, 2), (0, 5), (5, 0)],
        ),
        (
            CmpOp::Gt,
            CmpOp::Gt,
            vec![(1, 0), (4, 6)],
        ),
        (
            CmpOp::Ge,
            CmpOp::Ge,
            vec![
                (0, 0), (0, 5), (1, 0), (1, 5), (4, 0), (4, 5), (4, 6), (5, 0),
            ],
        ),
    ];

    for (op1, op2, want) in expected {
        let plan = validate(op1, op2, &left, &right).unwrap();
        let (ie, ie_m) = ie_pairs(&plan, &left, &right).unwrap();
        let (nl, nl_m) = nl_pairs(&plan, &left, &right).unwrap();
        let bf = brute_force(op1, op2, &left, &right);

        assert_eq!(ie, want, "IE literal mismatch for {op1:?}/{op2:?}");
        assert_eq!(nl, want, "nested loop literal mismatch for {op1:?}/{op2:?}");
        assert_eq!(bf, want, "brute force literal mismatch for {op1:?}/{op2:?}");

        // Counter sanity for a non-trivial run.
        assert_eq!(ie_m.pairs_emitted, want.len() as u64);
        assert_eq!(
            nl_m.nl_pair_evaluations,
            (left.row_count() * right.row_count()) as u64,
        );
        assert_eq!(nl_m.pairs_emitted, want.len() as u64);
        // Only left rows matchable on both keys enter the IEJoin sweep.
        assert!(ie_m.left_rows_scanned <= left.row_count() as u64);
        assert_eq!(ie_m.nl_pair_evaluations, 0);
    }
}

#[test]
fn all_sixteen_operator_combinations_agree_with_brute_force() {
    let (left, right) = mixed().unwrap();
    let ops = [CmpOp::Lt, CmpOp::Le, CmpOp::Gt, CmpOp::Ge];
    for &op1 in &ops {
        for &op2 in &ops {
            let plan = validate(op1, op2, &left, &right).unwrap();
            let (ie, _) = ie_pairs(&plan, &left, &right).unwrap();
            let (nl, _) = nl_pairs(&plan, &left, &right).unwrap();
            let bf = brute_force(op1, op2, &left, &right);
            assert_eq!(ie, bf, "IE differs from brute force: {op1:?}/{op2:?}");
            assert_eq!(nl, bf, "NL differs from brute force: {op1:?}/{op2:?}");
        }
    }
}

#[test]
fn random_fixtures_agree_with_brute_force_across_seeds_and_directions() {
    let ops = [CmpOp::Lt, CmpOp::Le, CmpOp::Gt, CmpOp::Ge];
    for seed in 0..25u64 {
        let ln = 3 + (seed as usize) % 9;
        let rn = 2 + (seed as usize) % 11;
        let span = 1 + seed % 6;
        let f = RangeFixture::random_broad(seed, ln, rn, span).unwrap();
        for &op1 in &ops {
            for &op2 in &ops {
                let plan = RangeFixture::predicates(op1, op2);
                let plan =
                    iejoin_backend::JoinPlan::validate(plan, &f.left, &f.right).unwrap();
                let (ie, _) = ie_pairs(&plan, &f.left, &f.right).unwrap();
                let bf = brute_force(op1, op2, &f.left, &f.right);
                assert_eq!(
                    ie, bf,
                    "seed {seed} {op1:?}/{op2:?} (l={ln} r={rn} span={span})"
                );
            }
        }
    }
}

#[test]
fn all_equal_strict_vs_nonstrict_boundaries_and_multiplicity() {
    let n = 4usize;
    let f = RangeFixture::all_equal(n).unwrap();

    // Strict predicates: nothing matches on the all-equal column.
    for strict in [CmpOp::Lt, CmpOp::Gt] {
        let plan = iejoin_backend::JoinPlan::validate(
            RangeFixture::predicates(strict, strict),
            &f.left,
            &f.right,
        )
        .unwrap();
        let (pairs, m) = ie_pairs(&plan, &f.left, &f.right).unwrap();
        assert!(pairs.is_empty(), "{strict:?} must reject equal values");
        assert_eq!(m.pairs_emitted, 0);
    }

    // Non-strict: every one of the n*n identity pairs survives, with full
    // Cartesian multiplicity (duplicate rows keep distinct identities).
    for incl in [CmpOp::Le, CmpOp::Ge] {
        let plan = iejoin_backend::JoinPlan::validate(
            RangeFixture::predicates(incl, incl),
            &f.left,
            &f.right,
        )
        .unwrap();
        let (pairs, _) = ie_pairs(&plan, &f.left, &f.right).unwrap();
        let bf = brute_force(incl, incl, &f.left, &f.right);
        assert_eq!(pairs.len(), n * n, "{incl:?} must be a full cartesian product");
        assert_eq!(pairs, bf);
        // Every identity pair appears exactly once.
        let mut dedup = pairs.clone();
        dedup.sort_unstable();
        dedup.dedup();
        assert_eq!(dedup.len(), n * n);
    }

    // Mixed strict/non-strict on equal keys is always empty here.
    let plan = iejoin_backend::JoinPlan::validate(
        RangeFixture::predicates(CmpOp::Lt, CmpOp::Le),
        &f.left,
        &f.right,
    )
    .unwrap();
    let (pairs, _) = ie_pairs(&plan, &f.left, &f.right).unwrap();
    assert!(pairs.is_empty());
}

#[test]
fn empty_either_side_yields_no_pairs_and_no_work() {
    for (le, re) in [(true, false), (false, true), (true, true)] {
        let f = RangeFixture::empty_side(le, re).unwrap();
        let plan = iejoin_backend::JoinPlan::validate(
            RangeFixture::predicates(CmpOp::Lt, CmpOp::Le),
            &f.left,
            &f.right,
        )
        .unwrap();
        let (pairs, m) = ie_pairs(&plan, &f.left, &f.right).unwrap();
        let (nl_pairs, nl_m) = nl_pairs(&plan, &f.left, &f.right).unwrap();
        assert!(pairs.is_empty(), "empty side {le}/{re}");
        assert!(nl_pairs.is_empty());
        assert_eq!(m.pairs_emitted, 0);
        assert_eq!(m.left_rows_scanned, 0);
        assert_eq!(nl_m.nl_pair_evaluations, 0);
    }
}

/// Hand-built high-selectivity fixtures with exact counters.
mod selectivity {
    use super::*;

    /// Left keys in [1,2], right keys in [10,12]: ascending predicates
    /// qualify most pairs, descending ones almost none.
    fn dense_ascending() -> (TypedBatch, TypedBatch) {
        let left = TypedBatch::new(vec![
            i64col("a1", vec![Some(1), Some(2), Some(1), Some(2)]),
            i64col("a2", vec![Some(1), Some(2), Some(1), Some(2)]),
        ])
        .unwrap();
        let right = TypedBatch::new(vec![
            i64col("b1", vec![Some(10), Some(11), Some(12), Some(10), Some(11), Some(12)]),
            i64col("b2", vec![Some(10), Some(11), Some(12), Some(10), Some(11), Some(12)]),
        ])
        .unwrap();
        (left, right)
    }

    #[test]
    fn dense_case_ascending_has_concrete_pairs_and_access_counts() {
        let (left, right) = dense_ascending();
        let plan = validate(CmpOp::Lt, CmpOp::Lt, &left, &right).unwrap();
        let (pairs, m) = ie_pairs(&plan, &left, &right).unwrap();
        let bf = brute_force(CmpOp::Lt, CmpOp::Lt, &left, &right);

        // Every left value is smaller than every right value: 4*6 = 22.
        assert_eq!(pairs.len(), 24);
        assert_eq!(pairs, bf);

        // Exact candidate-accounting expectations:
        assert_eq!(m.left_rows_scanned, 4, "all 4 left rows matchable");
        assert_eq!(m.p1_right_rows_inserted, 6, "all 6 right rows inserted once");
        // Outer a1 groups in descending order are {2}:2 rows then {1}:2 rows.
        // p2 prefix lengths for a2 keys 2,2,1,1 are 6,6,6,6 (b2 all bigger).
        assert_eq!(m.p2_probe_positions, 24);
        assert_eq!(m.p2_words_scanned, 4, "one 64-bit word per probe");
        assert_eq!(m.pairs_emitted, 24);

        // IEJoin probes far fewer positions than a nested loop evaluates.
        let (_, nl_m) = nl_pairs(&plan, &left, &right).unwrap();
        assert_eq!(nl_m.nl_pair_evaluations, 24);
        assert!(m.p2_probe_positions <= nl_m.nl_pair_evaluations);
    }

    #[test]
    fn dense_case_descending_is_sparse_with_concrete_counts() {
        let (left, right) = dense_ascending();
        let plan = validate(CmpOp::Gt, CmpOp::Gt, &left, &right).unwrap();
        let (pairs, m) = ie_pairs(&plan, &left, &right).unwrap();
        assert!(pairs.is_empty(), "no left value exceeds any right value");
        assert_eq!(m.pairs_emitted, 0);
        // a1 ascending outer groups {1},{2}; no b1 (>=10) qualifies, so no
        // right row is ever inserted and no prefix is probed.
        assert_eq!(m.p1_right_rows_inserted, 0);
        assert_eq!(m.p2_probe_positions, 0);
        assert_eq!(m.p2_words_scanned, 0);
    }

    /// Mostly-disjoint ranges with a narrow overlap band: genuinely sparse
    /// matches in the ascending direction.
    #[test]
    fn sparse_overlap_band_has_exact_pair_set() {
        // left a1: high 100s; right b1: low values with a few 100s.
        let left = TypedBatch::new(vec![
            i64col("a1", vec![Some(100), Some(101), Some(102)]),
            i64col("a2", vec![Some(100), Some(101), Some(102)]),
        ])
        .unwrap();
        let right = TypedBatch::new(vec![
            i64col(
                "b1",
                vec![Some(1), Some(100), Some(2), Some(101), Some(100), Some(3)],
            ),
            i64col(
                "b2",
                vec![Some(1), Some(100), Some(2), Some(101), Some(100), Some(3)],
            ),
        ])
        .unwrap();

        // Gt: left 100 beats right {1,2,3} (r0,r2,r5); left 101 additionally
        // beats the two 100s (r1,r4); left 102 additionally beats the 101 r3.
        let plan = validate(CmpOp::Gt, CmpOp::Gt, &left, &right).unwrap();
        let (pairs, m) = ie_pairs(&plan, &left, &right).unwrap();
        let want = vec![
            (0u32, 0u32), (0, 2), (0, 5),
            (1, 0), (1, 1), (1, 2), (1, 4), (1, 5),
            (2, 0), (2, 1), (2, 2), (2, 3), (2, 4), (2, 5),
        ];
        let bf = brute_force(CmpOp::Gt, CmpOp::Gt, &left, &right);
        assert_eq!(pairs, want);
        assert_eq!(bf, want);
        assert_eq!(m.pairs_emitted, 14);
        // 14 matches out of 18 candidate pairs: high selectivity confirmed.
        assert!(m.pairs_emitted < (3 * 6) as u64);
    }
}
