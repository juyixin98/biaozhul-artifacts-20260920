//! SQL three-valued logic (TRUE / FALSE / UNKNOWN) as *three sets over one
//! explicit document universe*.
//!
//! A [`TriPartition`] stores `true` and `false` bitmaps; `unknown` is derived as
//! `universe MINUS (true UNION false)`. The universe is explicit because
//! versioned deletions remove documents from the universe: a deleted row is
//! neither true nor false nor unknown — it is simply not in the universe.
//!
//! Boundary rules implemented here, on purpose:
//! * `NOT` swaps the true/false bitmaps and leaves unknown in place. It never
//!   takes a machine-word complement of the true bitmap (that would turn
//!   unknown *and deleted* rows into false).
//! * `AND` / `OR` combine only rows both partitions place in the *same*
//!   universe.
//!
//! Kleene truth tables implemented by the set formulas below:
//!
//! | AND | T | F | U |   | OR | T | F | U |
//! |-----|---|---|---|---|----|---|---|---|
//! | T   | T | F | U |   | T  | T | T | T |
//! | F   | F | F | F |   | F  | T | F | U |
//! | U   | U | F | U |   | U  | T | U | U |

use crate::bits::WordBitmap;
use crate::error::{ErrorKind, IndexError, Result};

/// Scalar Kleene truth value, used for per-row evaluation and logging.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TriState {
    True,
    False,
    Unknown,
}

impl TriState {
    pub fn as_str(self) -> &'static str {
        match self {
            TriState::True => "TRUE",
            TriState::False => "FALSE",
            TriState::Unknown => "UNKNOWN",
        }
    }

    /// Kleene NOT.
    pub fn not(self) -> Self {
        match self {
            TriState::True => TriState::False,
            TriState::False => TriState::True,
            TriState::Unknown => TriState::Unknown,
        }
    }

    /// Kleene AND.
    pub fn and(self, other: Self) -> Self {
        use TriState::*;
        match (self, other) {
            (False, _) | (_, False) => False,
            (True, True) => True,
            _ => Unknown,
        }
    }

    /// Kleene OR.
    pub fn or(self, other: Self) -> Self {
        use TriState::*;
        match (self, other) {
            (True, _) | (_, True) => True,
            (False, False) => False,
            _ => Unknown,
        }
    }
}

/// A partition of one universe into true/false/unknown sets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TriPartition {
    /// The document universe this partition is defined on (live rows at a
    /// version). Every set bit of `t`, `f`, and `u` lies inside it.
    universe: WordBitmap,
    t: WordBitmap,
    f: WordBitmap,
}

impl TriPartition {
    /// Construct from true/false sets and force both inside `universe`.
    /// Rows outside `t ∪ f` but inside the universe become unknown.
    pub fn new(universe: WordBitmap, t: WordBitmap, f: WordBitmap) -> Result<Self> {
        let mut p = Self { universe, t, f };
        p.normalize();
        p.validate()?;
        Ok(p)
    }

    /// Everything in the universe is unknown (e.g. `NULL OR NULL` on all rows).
    pub fn all_unknown(universe: WordBitmap) -> Self {
        Self { t: WordBitmap::zeros(universe.len()), f: WordBitmap::zeros(universe.len()), universe }
    }

    /// Everything in the universe is true / false.
    pub fn constant(universe: WordBitmap, value: TriState) -> Self {
        let len = universe.len();
        match value {
            TriState::True => Self { t: universe.clone(), f: WordBitmap::zeros(len), universe },
            TriState::False => Self { t: WordBitmap::zeros(len), f: universe.clone(), universe },
            TriState::Unknown => Self::all_unknown(universe),
        }
    }

    fn normalize(&mut self) {
        // Sets cannot reach outside the universe. Intersecting with the
        // (tail-clean) universe mask also keeps tail bits zero.
        self.t = self.t.intersect(&self.universe).expect("lengths equal by construction");
        self.f = self.f.intersect(&self.universe).expect("lengths equal by construction");
        // True and false are contradictory; if both claim a row, that is an
        // input error surfaced in `validate`, never silently resolved.
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.universe.len()
    }

    pub fn universe(&self) -> &WordBitmap {
        &self.universe
    }
    pub fn true_set(&self) -> &WordBitmap {
        &self.t
    }
    pub fn false_set(&self) -> &WordBitmap {
        &self.f
    }

    /// UNKNOWN = universe \ (true ∪ false).
    pub fn unknown_set(&self) -> WordBitmap {
        let claimed = self.t.union(&self.f).expect("same length");
        self.universe.and_not(&claimed).expect("same length")
    }

    pub fn counts(&self) -> TriCounts {
        TriCounts { true_rows: self.t.count(), false_rows: self.f.count(), unknown_rows: self.unknown_set().count() }
    }

    /// Per-row classification; returns `None` for rows outside the universe
    /// (deleted / non-existent).
    pub fn classify(&self, row: usize) -> Option<TriState> {
        if !self.universe.get(row) {
            return None;
        }
        if self.t.get(row) {
            Some(TriState::True)
        } else if self.f.get(row) {
            Some(TriState::False)
        } else {
            Some(TriState::Unknown)
        }
    }

    /// Structural invariant:
    /// 1. all three bitmaps share the universe length and have clean tails;
    /// 2. t ⊆ universe, f ⊆ universe;
    /// 3. t ∩ f = ∅;
    /// 4. t ⊎ f ⊎ u = universe (every live row is in exactly one set).
    pub fn validate(&self) -> Result<()> {
        self.universe.validate()?;
        self.t.validate()?;
        self.f.validate()?;
        if self.t.len() != self.universe.len() || self.f.len() != self.universe.len() {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "tri-partition: true/false length differs from universe length",
            ));
        }
        if !self.t.is_disjoint(&self.f)? {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "tri-partition: a row is simultaneously TRUE and FALSE",
            ));
        }
        let t_outside = self.t.and_not(&self.universe)?;
        let f_outside = self.f.and_not(&self.universe)?;
        if t_outside.count() != 0 || f_outside.count() != 0 {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "tri-partition: TRUE/FALSE sets contain rows outside the document universe",
            ));
        }
        let claimed = self.t.union(&self.f)?;
        let u = self.universe.and_not(&claimed)?;
        let partition_union = claimed.union(&u)?;
        if partition_union != self.universe {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "tri-partition: TRUE/FALSE/UNKNOWN do not recombine into the universe",
            ));
        }
        Ok(())
    }

    fn check_compatible(&self, other: &Self, op: &str) -> Result<()> {
        if self.universe != other.universe {
            return Err(IndexError::new(
                ErrorKind::LengthMismatch,
                format!("{op}: operands live in different universes (different deletions applied?)"),
            ));
        }
        Ok(())
    }

    /// Kleene AND: TRUE only where both are true; FALSE where either is false.
    pub fn and(&self, other: &Self) -> Result<Self> {
        self.check_compatible(other, "AND")?;
        let t = self.t.intersect(&other.t)?;
        let f = self.f.union(&other.f)?;
        Self::new(self.universe.clone(), t, f)
    }

    /// Kleene OR: TRUE where either is true; FALSE only where both are false.
    pub fn or(&self, other: &Self) -> Result<Self> {
        self.check_compatible(other, "or")?;
        let t = self.t.union(&other.t)?;
        let f = self.f.intersect(&other.f)?;
        Self::new(self.universe.clone(), t, f)
    }

    /// Kleene NOT: swap TRUE and FALSE, keep UNKNOWN.
    ///
    /// Deliberately NOT `!self.t` — a wordwise complement would mark every
    /// unknown and deleted/universe-external row as false.
    pub fn not(&self) -> Result<Self> {
        Self::new(self.universe.clone(), self.f.clone(), self.t.clone())
    }

    /// Restrict a partition to a smaller universe (used when a query runs at a
    /// read version where more rows are deleted). Rows absent from the new
    /// universe disappear from all three sets.
    pub fn restrict_universe(&self, universe: WordBitmap) -> Result<Self> {
        if universe.len() != self.universe.len() {
            return Err(IndexError::new(
                ErrorKind::LengthMismatch,
                "restrict_universe: physical length cannot change",
            ));
        }
        // New universe must be a subset of the old one (undeleting is a
        // different operation and must not pass through here).
        if universe.and_not(&self.universe)?.count() != 0 {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "restrict_universe: new universe contains rows absent from the old one",
            ));
        }
        Self::new(universe, self.t.clone(), self.f.clone())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TriCounts {
    pub true_rows: usize,
    pub false_rows: usize,
    pub unknown_rows: usize,
}

#[cfg(test)]
mod tests {
    use super::*;
    use TriState::{False, True, Unknown};

    fn p(len: usize, t: &[usize], f: &[usize]) -> TriPartition {
        TriPartition::new(
            WordBitmap::ones(len),
            WordBitmap::from_indices(len, t.iter().copied()).unwrap(),
            WordBitmap::from_indices(len, f.iter().copied()).unwrap(),
        )
        .unwrap()
    }

    #[test]
    fn kleene_scalar_tables() {
        use TriState::*;
        assert_eq!(Unknown.not(), Unknown);
        assert_eq!(True.not(), False);
        assert_eq!(True.and(Unknown), Unknown);
        assert_eq!(False.and(Unknown), False);
        assert_eq!(Unknown.and(Unknown), Unknown);
        assert_eq!(True.or(Unknown), True);
        assert_eq!(False.or(Unknown), Unknown);
        assert_eq!(Unknown.or(Unknown), Unknown);
    }

    #[test]
    fn not_never_promotes_unknown_to_false() {
        // Universe of 5: rows 0=T, 1=F, rows 2,3,4=U.
        let x = p(5, &[0], &[1]);
        let nx = x.not().unwrap();
        assert_eq!(nx.classify(0), Some(False));
        assert_eq!(nx.classify(1), Some(True));
        assert_eq!(nx.classify(2), Some(Unknown));
        assert_eq!(nx.classify(3), Some(Unknown));
        assert_eq!(nx.classify(4), Some(Unknown));
        nx.validate().unwrap();
        let nnx = nx.not().unwrap();
        assert_eq!(nnx, x, "double negation is identity");
    }

    #[test]
    fn and_or_on_non_word_universe_keep_exhaustive_partition() {
        // 65 rows -> two words with a single valid tail bit.
        let x = p(65, &[0, 64], &[1]);
        let y = p(65, &[0, 2], &[64]);
        let a = x.and(&y).unwrap();
        let o = x.or(&y).unwrap();
        a.validate().unwrap();
        o.validate().unwrap();
        // Tail row 64: x=T, y=F => AND F, OR T.
        assert_eq!(a.classify(64), Some(False));
        assert_eq!(o.classify(64), Some(True));
        let c = a.counts();
        assert_eq!(c.true_rows + c.false_rows + c.unknown_rows, 65);
    }

    #[test]
    fn incompatible_universes_are_rejected() {
        let a = p(10, &[0], &[1]);
        let mut smaller = WordBitmap::ones(10);
        smaller = smaller.and_not(&WordBitmap::from_indices(10, [2]).unwrap()).unwrap();
        let b = TriPartition::new(smaller, WordBitmap::zeros(10), WordBitmap::zeros(10)).unwrap();
        assert_eq!(a.and(&b).unwrap_err().kind, ErrorKind::LengthMismatch);
    }

    #[test]
    fn deleted_rows_belong_to_no_set() {
        let full = WordBitmap::ones(10);
        let mut live = full.clone();
        live = live.and_not(&WordBitmap::from_indices(10, [3, 7]).unwrap()).unwrap();
        let x = TriPartition::new(
            full,
            WordBitmap::from_indices(10, [0, 3]).unwrap(),
            WordBitmap::zeros(10),
        )
        .unwrap();
        let at_v1 = x.restrict_universe(live).unwrap();
        at_v1.validate().unwrap();
        assert_eq!(at_v1.classify(3), None, "deleted row 3 is outside all three sets");
        assert_eq!(at_v1.classify(7), None);
        assert_eq!(at_v1.classify(0), Some(True));
        let c = at_v1.counts();
        assert_eq!(c.true_rows + c.false_rows + c.unknown_rows, 8);
    }
}
