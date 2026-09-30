//! Deterministic, fully synthetic input fixtures.
//!
//! Every fixture is generated from a fixed-seed LCG and explicit hand-built
//! edge cases. There are no external participants, random OS entropy, files or
//! network calls, so test replays are byte-for-byte reproducible.

use crate::batch::{Column, TypedBatch};
use crate::error::Result;
use crate::operator::plan::{CmpOp, JoinPredicate};

/// Numerical LCG (Numerical Recipes constants), advanced purely deterministically.
#[derive(Debug, Clone)]
pub struct Lcg {
    state: u64,
}

impl Lcg {
    /// Start the generator from an explicit seed.
    #[must_use]
    pub fn new(seed: u64) -> Self {
        Lcg { state: seed }
    }

    /// Next 64-bit draw.
    pub fn next_u64(&mut self) -> u64 {
        // Numerical Recipes modulo 2^64.
        self.state = self
            .state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        self.state
    }

    /// Uniform draw in `0..n` (n must be > 0).
    pub fn below(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }
}

/// One generated join workload with its provenance.
#[derive(Debug, Clone)]
pub struct RangeFixture {
    pub name: String,
    pub seed: u64,
    pub left: TypedBatch,
    pub right: TypedBatch,
}

impl RangeFixture {
    /// The two predicates: `left.a1 OP1 right.b1 AND left.a2 OP2 right.b2`.
    #[must_use]
    pub fn predicates(op1: CmpOp, op2: CmpOp) -> [JoinPredicate; 2] {
        [
            JoinPredicate { left_column: "a1".into(), op: op1, right_column: "b1".into() },
            JoinPredicate { left_column: "a2".into(), op: op2, right_column: "b2".into() },
        ]
    }
}

fn build_int_fixture(
    name: &str,
    seed: u64,
    left_n: usize,
    right_n: usize,
    value_span: u64,
    null_every: Option<u64>,
) -> Result<RangeFixture> {
    let mut rng = Lcg::new(seed);
    let mut gen = |n: usize| -> Vec<Option<i64>> {
        (0..n)
            .map(|i| {
                let draw = rng.below(value_span.max(1)) as i64;
                let null_here = null_every.is_some_and(|period| period > 0 && i as u64 % period == 0);
                if null_here { None } else { Some(draw) }
            })
            .collect()
    };
    let la1 = gen(left_n);
    let la2 = gen(left_n);
    let rb1 = gen(right_n);
    let rb2 = gen(right_n);

    let left = TypedBatch::new(vec![
        ("a1".to_owned(), Column::from_i64(la1)),
        ("a2".to_owned(), Column::from_i64(la2)),
    ])?;
    let right = TypedBatch::new(vec![
        ("b1".to_owned(), Column::from_i64(rb1)),
        ("b2".to_owned(), Column::from_i64(rb2)),
    ])?;
    Ok(RangeFixture { name: name.to_owned(), seed, left, right })
}

impl RangeFixture {
    /// All rows have the same key value on both keys: with strict predicates
    /// nothing matches; with non-strict predicates every non-NULL pair matches.
    /// Exercises equal-group boundaries directly.
    pub fn all_equal(n: usize) -> Result<RangeFixture> {
        let val = vec![Some(7i64); n];
        let left = TypedBatch::new(vec![
            ("a1".to_owned(), Column::from_i64(val.clone())),
            ("a2".to_owned(), Column::from_i64(val.clone())),
        ])?;
        let right = TypedBatch::new(vec![
            ("b1".to_owned(), Column::from_i64(val.clone())),
            ("b2".to_owned(), Column::from_i64(val)),
        ])?;
        Ok(RangeFixture { name: "all_equal".into(), seed: 0, left, right })
    }

    /// One side (or both, when both flags are set) is empty.
    pub fn empty_side(left_empty: bool, right_empty: bool) -> Result<RangeFixture> {
        let l = if left_empty { vec![] } else { vec![Some(1i64), Some(2)] };
        let r = if right_empty { vec![] } else { vec![Some(3i64), Some(4)] };
        let left = TypedBatch::new(vec![
            ("a1".to_owned(), Column::from_i64(l.clone())),
            ("a2".to_owned(), Column::from_i64(l)),
        ])?;
        let right = TypedBatch::new(vec![
            ("b1".to_owned(), Column::from_i64(r.clone())),
            ("b2".to_owned(), Column::from_i64(r)),
        ])?;
        Ok(RangeFixture {
            name: format!("empty_{left_empty}_{right_empty}"),
            seed: 0,
            left,
            right,
        })
    }

    /// Hand-built mixed fixture: duplicates, NULLs, gaps and ties on small
    /// domains; exact expectations live in `tests/engine_eq.rs`.
    pub fn mixed_small() -> Result<RangeFixture> {
        // Rows:        0    1     2    3    4
        let la1 = vec![Some(2), Some(2), None, Some(0), Some(5)];
        let la2 = vec![Some(3), Some(1), Some(9), None, Some(0)];
        let rb1 = vec![Some(2), Some(4), Some(2), None, Some(9)];
        let rb2 = vec![Some(1), Some(2), Some(3), Some(8), None];
        let left = TypedBatch::new(vec![
            ("a1".to_owned(), Column::from_i64(la1)),
            ("a2".to_owned(), Column::from_i64(la2)),
        ])?;
        let right = TypedBatch::new(vec![
            ("b1".to_owned(), Column::from_i64(rb1)),
            ("b2".to_owned(), Column::from_i64(rb2)),
        ])?;
        Ok(RangeFixture { name: "mixed_small".into(), seed: 1, left, right })
    }

    /// High-selectivity pattern: left keys occupy a tiny low range, right keys
    /// a wide high range. For ascending predicates most pairs qualify on p1;
    /// for descending predicates almost none do. `spread` widens the right
    /// domain; set `descending_selective` for the mirrored sparse case.
    pub fn high_selectivity(n: usize, seed: u64, descending_selective: bool) -> Result<RangeFixture> {
        let mut rng = Lcg::new(seed);
        let (la, rb): (Vec<Option<i64>>, Vec<Option<i64>>) = (0..n)
            .map(|_| {
                let l = Some((rng.below(4)) as i64);
                let r = Some((100 + rng.below(1_000)) as i64);
                if descending_selective { (r, l) } else { (l, r) }
            })
            .unzip();
        let left = TypedBatch::new(vec![
            ("a1".to_owned(), Column::from_i64(la.clone())),
            ("a2".to_owned(), Column::from_i64(la)),
        ])?;
        let right = TypedBatch::new(vec![
            ("b1".to_owned(), Column::from_i64(rb.clone())),
            ("b2".to_owned(), Column::from_i64(rb)),
        ])?;
        Ok(RangeFixture {
            name: format!("high_selectivity_{descending_selective}"),
            seed,
            left,
            right,
        })
    }

    /// Broad random fixture with periodic NULLs: the general fuzzing oracle.
    pub fn random_broad(seed: u64, left_n: usize, right_n: usize, span: u64) -> Result<RangeFixture> {
        build_int_fixture("random_broad", seed, left_n, right_n, span, Some(7))
    }
}
