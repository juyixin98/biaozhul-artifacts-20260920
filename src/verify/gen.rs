//! Deterministic synthetic-case generator for differential verification.
//!
//! A tiny xorshift64 PRNG (no `rand` dependency) makes every run reproducible
//! from its seed — the seed is logged in every [`super::VerifyReport`], so any
//! failing input can be regenerated exactly.
//!
//! Cases deliberately cover the boundary shapes called out in the spec:
//! non-machine-word universe lengths (1, 7, 9, 63, 65, 67, 127, 129…),
//! heavy NULL density (so AND/OR/NOT all encounter NULL crossings),
//! all-UNKNOWN leaves, and versioned deletions of both NULL and non-NULL rows.

use crate::batch::{BatchBuilder, TypedBatch};
use crate::error::Result;

/// Minimal deterministic PRNG (xorshift64*). Independent from the engine.
#[derive(Debug, Clone)]
pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Self {
        // Avoid the forbidden zero state.
        Self {
            state: seed.max(1) ^ 0x9E37_79B9_7F4A_7C15,
        }
    }

    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// Uniform-ish value in `[0, n)`; `n` must be > 0.
    pub fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }

    pub fn chance(&mut self, numerator: u32, denominator: u32) -> bool {
        (self.next_u64() % denominator as u64) < numerator as u64
    }

    pub fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }
}

const CITIES: &[&str] = &["NY", "LA", "SF", "OSL", "HK", "TYO"];

/// One fully specified synthetic scenario.
#[derive(Debug, Clone)]
pub struct GeneratedCase {
    pub name: String,
    pub seed: u64,
    pub rows: usize,
    pub batch: TypedBatch,
    /// Source text of a random predicate — also exercises the query parser.
    pub expr_src: String,
    /// Row ids the harness intends to delete (subset of the row space).
    pub dead_rows: Vec<usize>,
}

/// Universe lengths used in rotation: all awkward around 64-bit boundaries.
pub const BOUNDARY_LENGTHS: &[usize] = &[1, 2, 7, 8, 9, 63, 64, 65, 67, 127, 128, 129, 200];

/// Generate a batch of `rows` with three typed columns and `null_density`
/// NULL probability (0..=100), plus a random expression and deletion set.
pub fn generate_case(name: impl Into<String>, seed: u64, rows: usize, null_density: u32) -> Result<GeneratedCase> {
    let mut rng = Rng::new(seed);
    let name = name.into();

    let mut ages: Vec<Option<i64>> = Vec::with_capacity(rows);
    let mut cities: Vec<Option<&str>> = Vec::with_capacity(rows);
    let mut vips: Vec<Option<bool>> = Vec::with_capacity(rows);
    for _ in 0..rows {
        ages.push(if rng.chance(null_density, 100) {
            None
        } else {
            // Small value pool guarantees repeated values (bitmap merges) and
            // frequent equalities with random literals.
            Some((rng.below(8) as i64) - 2)
        });
        cities.push(if rng.chance(null_density, 100) {
            None
        } else {
            Some(*rng.pick(CITIES))
        });
        vips.push(if rng.chance(null_density, 100) {
            None
        } else {
            Some(rng.chance(1, 2))
        });
    }

    let batch = BatchBuilder::new()
        .add_integer("age", ages)
        .add_text("city", cities)
        .add_boolean("vip", vips)
        .build()?;

    // At 100% NULL density every column is NULL everywhere. Comparison leaves
    // then evaluate to all-UNKNOWN, which is the intended "all-UNKNOWN"
    // scenario; `IS NULL` leaves would instead be all-TRUE and defeat it, so
    // the generator excludes null-test leaves in that mode.
    let all_null = null_density >= 100;
    let expr_src = random_expr(&mut rng, 2, all_null);

    // Delete a random 0..~25% of rows, always including a NULL row when one
    // exists, to prove deleted NULLs never become UNKNOWN.
    let mut dead_rows: Vec<usize> = Vec::new();
    if rows > 0 {
        let target = rng.below((rows / 4) + 1);
        for r in 0..rows {
            if dead_rows.len() < target && rng.chance(1, 4) {
                dead_rows.push(r);
            }
        }
        // Force-include row 0 with probability 1/2 (row 0 is frequently NULL).
        if rng.chance(1, 2) && rows > 1 && !dead_rows.contains(&0) {
            dead_rows.push(0);
        }
    }

    Ok(GeneratedCase {
        name,
        seed,
        rows,
        batch,
        expr_src,
        dead_rows,
    })
}

fn random_leaf(rng: &mut Rng, all_null: bool) -> String {
    // Four leaf shapes normally; at 100% NULL density only the first three
    // (comparisons) are admissible so all leaves are UNKNOWN.
    match rng.below(if all_null { 3 } else { 4 }) {
        0 => {
            let ops = ["=", "<>", "<", "<=", ">", ">="];
            format!("age {} {}", rng.pick(&ops), (rng.below(10) as i64) - 3)
        }
        1 => format!("city = '{}'", rng.pick(CITIES)),
        2 => format!("vip = {}", if rng.chance(1, 2) { "TRUE" } else { "FALSE" }),
        _ => {
            if rng.chance(1, 2) {
                "age IS NULL".to_string()
            } else {
                "city IS NOT NULL".to_string()
            }
        }
    }
}

fn random_expr(rng: &mut Rng, depth: u32, all_null: bool) -> String {
    if depth == 0 || rng.chance(2, 5) {
        return random_leaf(rng, all_null);
    }
    match rng.below(3) {
        0 => format!(
            "({} AND {})",
            random_expr(rng, depth - 1, all_null),
            random_expr(rng, depth - 1, all_null)
        ),
        1 => format!(
            "({} OR {})",
            random_expr(rng, depth - 1, all_null),
            random_expr(rng, depth - 1, all_null)
        ),
        _ => format!("(NOT {})", random_expr(rng, depth - 1, all_null)),
    }
}

/// Fixed boundary scenarios (not random) so the suite asserts exact results.
pub fn boundary_cases() -> Vec<(usize, u32)> {
    // (rows, null density)
    BOUNDARY_LENGTHS
        .iter()
        .map(|r| (*r, 45))
        .chain(std::iter::once((67, 100))) // all-NULL columns -> all UNKNOWN leaves
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generator_is_deterministic() {
        let a = generate_case("a", 42, 67, 40).unwrap();
        let b = generate_case("b", 42, 67, 40).unwrap();
        assert_eq!(a.expr_src, b.expr_src);
        assert_eq!(a.dead_rows, b.dead_rows);
        assert_eq!(a.batch.len(), b.batch.len());
    }

    #[test]
    fn generated_null_density_is_honoured() {
        let c = generate_case("c", 7, 500, 100).unwrap();
        let ages = c.batch.column_values("age").unwrap();
        assert!(ages.iter().all(|v| v.is_none()), "all-NULL case");
    }
}
