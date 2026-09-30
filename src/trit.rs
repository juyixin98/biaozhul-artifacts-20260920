//! SQL three-valued logic: `TRUE`, `FALSE`, `UNKNOWN`.
//!
//! `UNKNOWN` arises whenever a comparison or boolean operation involves SQL
//! `NULL`. The truth tables below are Kleene's strong three-valued logic /
//! SQL's semantics:
//!
//! ```text
//! AND   T F U      OR    T F U      NOT
//!   T   T F U        T   T T T       T -> F
//!   F   F F F        F   T F U       F -> T
//!   U   U F U        U   T U U       U -> U
//! ```
//!
//! Note in particular:
//! * `UNKNOWN AND FALSE = FALSE` (not unknown) — a single FALSE conjunct
//!   decides the conjunction.
//! * `UNKNOWN OR TRUE = TRUE` — a single TRUE disjunct decides the disjunction.
//! * `NOT UNKNOWN = UNKNOWN`, which is why `NOT` cannot be computed as a
//!   plain bitmap complement of only the TRUE set (see [`crate::partition`]).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Trit {
    True,
    False,
    Unknown,
}

impl Trit {
    pub const ALL: [Trit; 3] = [Trit::True, Trit::False, Trit::Unknown];

    #[inline]
    pub fn is_true(self) -> bool {
        matches!(self, Trit::True)
    }

    /// SQL three-valued `AND`.
    pub fn and(self, other: Trit) -> Trit {
        use Trit::*;
        match (self, other) {
            (False, _) | (_, False) => False,
            (True, True) => True,
            _ => Unknown,
        }
    }

    /// SQL three-valued `OR`.
    pub fn or(self, other: Trit) -> Trit {
        use Trit::*;
        match (self, other) {
            (True, _) | (_, True) => True,
            (False, False) => False,
            _ => Unknown,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Trit::True => "TRUE",
            Trit::False => "FALSE",
            Trit::Unknown => "UNKNOWN",
        }
    }
}

/// SQL three-valued `NOT`. `NOT UNKNOWN` stays `UNKNOWN`.
impl std::ops::Not for Trit {
    type Output = Trit;

    fn not(self) -> Trit {
        use Trit::*;
        match self {
            True => False,
            False => True,
            Unknown => Unknown,
        }
    }
}

impl std::fmt::Display for Trit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reference truth table transcribed independently from the SQL standard.
    #[test]
    fn kleene_truth_tables() {
        use Trit::*;
        for a in Trit::ALL {
            for b in Trit::ALL {
                // AND: TRUE iff both TRUE; FALSE iff any FALSE; else UNKNOWN.
                let expect_and = match (a, b) {
                    (True, True) => True,
                    (False, _) | (_, False) => False,
                    _ => Unknown,
                };
                assert_eq!(a.and(b), expect_and, "{a} AND {b}");
                assert_eq!(b.and(a), expect_and, "commutative AND");

                // OR: FALSE iff both FALSE; TRUE iff any TRUE; else UNKNOWN.
                let expect_or = match (a, b) {
                    (False, False) => False,
                    (True, _) | (_, True) => True,
                    _ => Unknown,
                };
                assert_eq!(a.or(b), expect_or, "{a} OR {b}");
                assert_eq!(b.or(a), expect_or, "commutative OR");
            }
            assert_eq!(!!a, a, "double negation {a}");
        }
        assert_eq!(!Unknown, Unknown);
        // The two rows that distinguish Kleene logic from naive NULL-propagation:
        assert_eq!(Unknown.and(False), False);
        assert_eq!(Unknown.or(True), True);
    }
}
