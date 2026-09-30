//! A three-set partition `{TRUE, FALSE, UNKNOWN}` of an explicit document
//! **domain**. The domain is itself a bitmap over the row-id space `[0, n)`:
//! every LIVE row belongs to EXACTLY ONE of the three sets, and deleted rows
//! appear in NONE of them because they are absent from the domain (see
//! [`crate::deletion`]).
//!
//! # Why three bitmaps instead of one plus `NOT`
//!
//! With a single TRUE bitmap there is no way to distinguish FALSE rows from
//! UNKNOWN (NULL-affected) rows, and a word-wise `!true_bits` would:
//! 1. fold FALSE and UNKNOWN together,
//! 2. set invalid tail bits past the end of the row-id space,
//! 3. re-admit deleted rows into UNKNOWN.
//!
//! Keeping `{domain, TRUE, FALSE, UNKNOWN}` explicit makes every distinction
//! structural: [`TritPartition::not`] swaps TRUE and FALSE and leaves UNKNOWN
//! (and the domain) untouched.

use std::collections::HashMap;

use crate::bitset::Bitmap;
use crate::trit::Trit;

#[derive(Clone, PartialEq, Eq)]
pub struct TritPartition {
    /// Size of the underlying row-id space.
    n: usize,
    /// Live document universe; T ∪ F ∪ U == domain and the sets are pairwise
    /// disjoint. Deleted rows are zero in `domain` and in all three sets.
    domain: Bitmap,
    sets: HashMap<Trit, Bitmap>,
}

impl std::fmt::Debug for TritPartition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TritPartition")
            .field("n", &self.n)
            .field("domain", &self.domain.count_ones())
            .field("true", &self.sets[&Trit::True].count_ones())
            .field("false", &self.sets[&Trit::False].count_ones())
            .field("unknown", &self.sets[&Trit::Unknown].count_ones())
            .finish()
    }
}

/// Concrete failure category for partition construction / combination.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum PartitionError {
    #[error("set for {which:?} has row-id space {got}, expected {expected}")]
    UniverseMismatch {
        which: Trit,
        expected: usize,
        got: usize,
    },
    #[error("domains differ in {op}: left has {left} live rows, right has {right}")]
    DomainMismatch {
        op: String,
        left: usize,
        right: usize,
    },
    #[error("{a:?} and {b:?} sets overlap at {count} position(s), first: {first}")]
    Overlap {
        a: Trit,
        b: Trit,
        count: usize,
        first: usize,
    },
    #[error("row {row} is in {which:?} but outside the live domain (deleted rows cannot be {which:?})")]
    OutsideDomain { which: Trit, row: usize },
    #[error("union of the three sets has {covered} live rows, domain has {domain} (first uncovered: {first_uncovered:?})")]
    NotExhaustive {
        covered: usize,
        domain: usize,
        first_uncovered: Option<usize>,
    },
}

impl TritPartition {
    /// Assemble a partition over the FULL row-id space `[0, n)` and validate.
    pub fn new(t: Bitmap, f: Bitmap, u: Bitmap) -> Result<Self, PartitionError> {
        let n = t.len();
        let domain = Bitmap::all(n);
        Self::with_domain(domain, t, f, u)
    }

    /// Assemble a partition over an explicit live domain (e.g. rows surviving
    /// versioned deletions) and validate it categorically.
    pub fn with_domain(
        domain: Bitmap,
        t: Bitmap,
        f: Bitmap,
        u: Bitmap,
    ) -> Result<Self, PartitionError> {
        let n = domain.len();
        let sets = HashMap::from([(Trit::True, t), (Trit::False, f), (Trit::Unknown, u)]);
        let p = Self { n, domain, sets };
        p.validate()?;
        Ok(p)
    }

    fn from_valid_parts(n: usize, domain: Bitmap, sets: HashMap<Trit, Bitmap>) -> Self {
        let p = Self { n, domain, sets };
        debug_assert!(p.validate().is_ok(), "algebraically built partition must be valid");
        p
    }

    /// Constant predicate over the full row-id space.
    pub fn constant(n: usize, value: Trit) -> Self {
        Self::constant_on(&Bitmap::all(n), value)
    }

    /// Constant predicate restricted to an explicit live domain.
    pub fn constant_on(domain: &Bitmap, value: Trit) -> Self {
        let n = domain.len();
        let mut sets = HashMap::new();
        for trit in Trit::ALL {
            sets.insert(
                trit,
                if trit == value {
                    domain.clone()
                } else {
                    Bitmap::new(n)
                },
            );
        }
        Self::from_valid_parts(n, domain.clone(), sets)
    }

    pub fn row_space_len(&self) -> usize {
        self.n
    }

    pub fn domain(&self) -> &Bitmap {
        &self.domain
    }

    pub fn set(&self, t: Trit) -> &Bitmap {
        &self.sets[&t]
    }

    pub fn true_set(&self) -> &Bitmap {
        self.set(Trit::True)
    }

    /// Rows satisfying the predicate — SQL WHERE keeps only TRUE. Deleted rows
    /// are never included.
    pub fn selected(&self) -> &Bitmap {
        self.true_set()
    }

    pub fn get(&self, row: usize) -> Option<Trit> {
        if !self.domain.get(row) {
            return None; // deleted / outside universe
        }
        for t in Trit::ALL {
            if self.sets[&t].get(row) {
                return Some(t);
            }
        }
        unreachable!("validated partition covers live row {row}");
    }

    /// Per-row truth values over the full row-id space; deleted rows are `None`.
    /// Used by the independent oracle cross-check.
    pub fn to_row_values(&self) -> Vec<Option<Trit>> {
        (0..self.n)
            .map(|i| {
                if self.domain.get(i) {
                    Some(if self.sets[&Trit::True].get(i) {
                        Trit::True
                    } else if self.sets[&Trit::False].get(i) {
                        Trit::False
                    } else {
                        Trit::Unknown
                    })
                } else {
                    None
                }
            })
            .collect()
    }

    /// Validate disjointness, domain containment and exhaustiveness.
    pub fn validate(&self) -> Result<(), PartitionError> {
        for t in Trit::ALL {
            if self.sets[&t].len() != self.n {
                return Err(PartitionError::UniverseMismatch {
                    which: t,
                    expected: self.n,
                    got: self.sets[&t].len(),
                });
            }
            // Each set must be a subset of the live domain.
            let outside = self.sets[&t].difference(&self.domain).expect("same n");
            if !outside.is_empty_set() {
                return Err(PartitionError::OutsideDomain {
                    which: t,
                    row: outside.iter_ones().next().unwrap(),
                });
            }
        }
        let pairs = [
            (Trit::True, Trit::False),
            (Trit::True, Trit::Unknown),
            (Trit::False, Trit::Unknown),
        ];
        for (a, b) in pairs {
            let inter = self.sets[&a]
                .intersection(&self.sets[&b])
                .expect("equal row space");
            if !inter.is_empty_set() {
                return Err(PartitionError::Overlap {
                    a,
                    b,
                    count: inter.count_ones(),
                    first: inter.iter_ones().next().unwrap(),
                });
            }
        }
        let union = self.sets[&Trit::True]
            .union(&self.sets[&Trit::False])
            .and_then(|x| x.union(&self.sets[&Trit::Unknown]))
            .expect("equal row space");
        if union != self.domain {
            let uncovered = self.domain.difference(&union).expect("same n");
            return Err(PartitionError::NotExhaustive {
                covered: union.count_ones(),
                domain: self.domain.count_ones(),
                first_uncovered: uncovered.iter_ones().next(),
            });
        }
        Ok(())
    }

    fn check_compatible(&self, other: &Self, op: &str) -> Result<(), PartitionError> {
        if self.n != other.n {
            return Err(PartitionError::UniverseMismatch {
                which: Trit::True,
                expected: self.n,
                got: other.n,
            });
        }
        if self.domain != other.domain {
            return Err(PartitionError::DomainMismatch {
                op: op.to_string(),
                left: self.domain.count_ones(),
                right: other.domain.count_ones(),
            });
        }
        Ok(())
    }

    /// SQL three-valued `AND` on a shared live domain.
    ///
    /// ```text
    /// TRUE    = A.TRUE  ∩ B.TRUE
    /// FALSE   = A.FALSE ∪ B.FALSE
    /// UNKNOWN = domain ∖ (TRUE ∪ FALSE)
    /// ```
    pub fn and(&self, other: &Self) -> Result<Self, PartitionError> {
        self.check_compatible(other, "AND")?;
        let t = self.sets[&Trit::True]
            .intersection(&other.sets[&Trit::True])
            .unwrap();
        let f = self.sets[&Trit::False]
            .union(&other.sets[&Trit::False])
            .unwrap();
        // Domain-relative remainder: deleted rows can never land in UNKNOWN.
        let u = self
            .domain
            .difference(&t.union(&f).unwrap())
            .unwrap();
        let mut sets = HashMap::new();
        sets.insert(Trit::True, t);
        sets.insert(Trit::False, f);
        sets.insert(Trit::Unknown, u);
        Ok(Self::from_valid_parts(self.n, self.domain.clone(), sets))
    }

    /// SQL three-valued `OR` on a shared live domain.
    ///
    /// ```text
    /// FALSE   = A.FALSE ∩ B.FALSE
    /// TRUE    = A.TRUE  ∪ B.TRUE
    /// UNKNOWN = domain ∖ (TRUE ∪ FALSE)
    /// ```
    pub fn or(&self, other: &Self) -> Result<Self, PartitionError> {
        self.check_compatible(other, "OR")?;
        let f = self.sets[&Trit::False]
            .intersection(&other.sets[&Trit::False])
            .unwrap();
        let t = self.sets[&Trit::True]
            .union(&other.sets[&Trit::True])
            .unwrap();
        let u = self
            .domain
            .difference(&t.union(&f).unwrap())
            .unwrap();
        let mut sets = HashMap::new();
        sets.insert(Trit::True, t);
        sets.insert(Trit::False, f);
        sets.insert(Trit::Unknown, u);
        Ok(Self::from_valid_parts(self.n, self.domain.clone(), sets))
    }

    /// SQL three-valued `NOT`: swap TRUE and FALSE, UNKNOWN and domain
    /// unchanged (`NOT UNKNOWN = UNKNOWN`; deleted rows stay excluded).
    /// This is a set swap, never a machine-word negation.
    pub fn not(&self) -> Self {
        let mut sets = HashMap::new();
        sets.insert(Trit::True, self.sets[&Trit::False].clone());
        sets.insert(Trit::False, self.sets[&Trit::True].clone());
        sets.insert(Trit::Unknown, self.sets[&Trit::Unknown].clone());
        Self::from_valid_parts(self.n, self.domain.clone(), sets)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a partition over a live domain given per-row values; rows not in
    /// `alive` must be absent from `rows`.
    fn p_with_alive(alive: &[usize], n: usize, rows: &[(usize, Trit)]) -> TritPartition {
        let domain = Bitmap::from_indices(n, alive.iter().copied());
        let mut t = Bitmap::new(n);
        let mut f = Bitmap::new(n);
        let mut u = Bitmap::new(n);
        for (row, v) in rows {
            match v {
                Trit::True => t.set(*row),
                Trit::False => f.set(*row),
                Trit::Unknown => u.set(*row),
            }
        }
        TritPartition::with_domain(domain, t, f, u).unwrap()
    }

    #[test]
    fn invalid_partitions_are_rejected_with_categories() {
        let n = 5;
        let bad = TritPartition::new(
            Bitmap::from_indices(n, [0]),
            Bitmap::from_indices(n, [0, 1]),
            Bitmap::from_indices(n, [2, 3, 4]),
        );
        assert!(matches!(bad, Err(PartitionError::Overlap { .. })));

        let bad = TritPartition::new(
            Bitmap::from_indices(n, [0]),
            Bitmap::from_indices(n, [1]),
            Bitmap::from_indices(n, [2, 3]),
        );
        assert!(matches!(bad, Err(PartitionError::NotExhaustive { .. })));

        // Row 3 deleted (not in domain) but present in TRUE -> rejected.
        let bad = TritPartition::with_domain(
            Bitmap::from_indices(n, [0, 1, 2, 4]),
            Bitmap::from_indices(n, [3]),
            Bitmap::new(n),
            Bitmap::from_indices(n, [0, 1, 2, 4]),
        );
        assert!(matches!(bad, Err(PartitionError::OutsideDomain { .. })));
    }

    #[test]
    fn deletion_domain_combination_compatibility() {
        // n = 67 (non-machine-word length), rows 3 and 64 deleted.
        let n = 67;
        let alive: Vec<usize> = (0..n).filter(|i| ![3, 64].contains(i)).collect();
        let values: Vec<(usize, Trit)> = alive
            .iter()
            .map(|i| {
                let v = match i % 3 {
                    0 => Trit::True,
                    1 => Trit::False,
                    _ => Trit::Unknown,
                };
                (*i, v)
            })
            .collect();
        let p = p_with_alive(&alive, n, &values);

        // Deleted rows belong to no set.
        assert_eq!(p.get(3), None);
        assert_eq!(p.get(64), None);

        // NOT keeps deleted rows excluded and swaps the rest.
        let np = p.not();
        assert_eq!(np.get(3), None);
        assert_eq!(np.get(64), None);
        for i in &alive {
            assert_eq!(np.get(*i), Some(!p.get(*i).unwrap()));
        }

        // Combining with a partition over a different domain is a typed error.
        let other_alive: Vec<usize> = (0..n).filter(|i| *i != 5).collect();
        let q = TritPartition::constant_on(
            &Bitmap::from_indices(n, other_alive),
            Trit::True,
        );
        assert!(matches!(
            p.and(&q),
            Err(PartitionError::DomainMismatch { .. })
        ));

        // Same-domain combination never leaks deleted rows into any set.
        let q = TritPartition::constant_on(p.domain(), Trit::Unknown);
        let a = p.and(&q).unwrap();
        let o = p.or(&q).unwrap();
        for r in [3usize, 64] {
            for trit in Trit::ALL {
                assert!(!a.set(trit).get(r), "AND leaked deleted row {r}");
                assert!(!o.set(trit).get(r), "OR leaked deleted row {r}");
            }
        }
        a.validate().unwrap();
        o.validate().unwrap();
    }

    #[test]
    fn all_unknown_not_stays_all_unknown() {
        let p = TritPartition::constant(67, Trit::Unknown);
        let n = p.not();
        assert_eq!(n.set(Trit::Unknown).count_ones(), 67);
        assert_eq!(n.set(Trit::True).count_ones(), 0);
        assert_eq!(n.set(Trit::False).count_ones(), 0);
    }

    #[test]
    fn operators_match_scalar_kleene_logic() {
        // Every 3x3 combination on a shared full domain of 9 rows plus a tail
        // pad to a non-machine-word length (67), padding as constant TRUE.
        let mut left: Vec<Trit> = (0..9).map(|i| Trit::ALL[i / 3]).collect();
        left.resize(67, Trit::True);
        let mut right: Vec<Trit> = (0..9).map(|i| Trit::ALL[i % 3]).collect();
        right.resize(67, Trit::True);

        let build = |rows: &[Trit]| {
            let n = rows.len();
            let (mut t, mut f, mut u) = (Bitmap::new(n), Bitmap::new(n), Bitmap::new(n));
            for (i, v) in rows.iter().enumerate() {
                match v {
                    Trit::True => t.set(i),
                    Trit::False => f.set(i),
                    Trit::Unknown => u.set(i),
                }
            }
            TritPartition::new(t, f, u).unwrap()
        };
        let pa = build(&left);
        let pb = build(&right);

        let and_rows = pa.and(&pb).unwrap().to_row_values();
        let or_rows = pa.or(&pb).unwrap().to_row_values();
        let not_rows = pa.not().to_row_values();
        for i in 0..left.len() {
            assert_eq!(and_rows[i], Some(left[i].and(right[i])), "AND row {i}");
            assert_eq!(or_rows[i], Some(left[i].or(right[i])), "OR row {i}");
            assert_eq!(not_rows[i], Some(!left[i]), "NOT row {i}");
        }
    }
}
