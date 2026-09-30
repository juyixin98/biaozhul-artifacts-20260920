//! Deterministic synthetic data. No external systems, no randomness, no
//! production accounts: every cell is a pure function of its row index, so
//! any run anywhere produces byte-identical input — which is what makes the
//! verification suite reproducible.
//!
//! The fixture is deliberately hostile to boundary bugs:
//! * 65 rows — one row beyond a full machine word, so tail-bit handling is
//!   always exposed (row 64 is the lone valid bit of the second-word);
//! * NULLs in every column at distinct, coprime-ish periods so NULL sets
//!   intersect in non-trivial ways;
//! * an all-NULL column (`note`) so "everything is UNKNOWN" is testable;
//! * deletes at versions 1 and 2, including the tail row 64;
//! * a composite index on (`grp`, `tag`) for the combined strategy.

use crate::batch::{BatchSchema, ColumnSchema, ColumnType};
use crate::engine::Engine;
use crate::error::Result;
use crate::scalar::Scalar;

/// Row count of the fixture table: 64 + 1, a non-word-aligned universe.
pub const FIXTURE_ROWS: usize = 65;

pub const FIXTURE_TABLE: &str = "docs";

/// Rows deleted at version 1 (includes the tail row 64 on purpose).
pub const DELETE_V1: &[usize] = &[1, 64];
/// Rows deleted at version 2.
pub const DELETE_V2: &[usize] = &[10, 11, 12];

/// The fixture schema.
pub fn fixture_schema() -> BatchSchema {
    BatchSchema::new(vec![
        ColumnSchema { name: "id".into(), ty: ColumnType::Int },
        ColumnSchema { name: "grp".into(), ty: ColumnType::Int },
        ColumnSchema { name: "score".into(), ty: ColumnType::Float },
        ColumnSchema { name: "flag".into(), ty: ColumnType::Bool },
        ColumnSchema { name: "tag".into(), ty: ColumnType::Text },
        ColumnSchema { name: "note".into(), ty: ColumnType::Text },
    ])
    .expect("fixture schema is valid")
}

/// Column-major fixture cells, deterministic in `row`.
pub fn fixture_columns() -> Vec<Vec<Option<Scalar>>> {
    let cell = |row: usize, col: usize| -> Option<Scalar> {
        match col {
            // id: NULL every 8th row starting at 3
            0 => {
                if row % 8 == 3 {
                    None
                } else {
                    Some(Scalar::Int(row as i64))
                }
            }
            // grp: 0..=4 cycling, NULL every 11th row starting at 4
            1 => {
                if row % 11 == 4 {
                    None
                } else {
                    Some(Scalar::Int((row % 5) as i64))
                }
            }
            // score: 0.0..8.0 in 0.5 steps, NULL every 7th row starting at 2
            2 => {
                if row % 7 == 2 {
                    None
                } else {
                    Some(Scalar::Float((row % 17) as f64 / 2.0))
                }
            }
            // flag: alternating, NULL every 6th row starting at 5
            3 => {
                if row % 6 == 5 {
                    None
                } else {
                    Some(Scalar::Bool(row % 2 == 0))
                }
            }
            // tag: "t0".."t3" cycling, NULL every 9th row starting at 1
            4 => {
                if row % 9 == 1 {
                    None
                } else {
                    Some(Scalar::Text(format!("t{}", row % 4)))
                }
            }
            // note: always NULL — the all-unknown column
            5 => None,
            _ => unreachable!("fixture has 6 columns"),
        }
    };
    (0..6).map(|col| (0..FIXTURE_ROWS).map(|row| cell(row, col)).collect()).collect()
}

/// Install the fixture into an engine: create the table, append the rows in
/// two batches (to exercise multi-batch tables), register the composite
/// index and apply the versioned deletes.
pub fn install_fixture(engine: &mut Engine) -> Result<()> {
    engine.create_table(FIXTURE_TABLE, fixture_schema())?;
    let columns = fixture_columns();
    let split = 40;
    let first: Vec<Vec<Option<Scalar>>> = columns.iter().map(|c| c[..split].to_vec()).collect();
    let second: Vec<Vec<Option<Scalar>>> = columns.iter().map(|c| c[split..].to_vec()).collect();
    engine.append_columns(FIXTURE_TABLE, first)?;
    engine.append_columns(FIXTURE_TABLE, second)?;
    engine.register_composite(FIXTURE_TABLE, &["grp".to_string(), "tag".to_string()])?;
    engine.delete_rows(FIXTURE_TABLE, 1, DELETE_V1)?;
    engine.delete_rows(FIXTURE_TABLE, 2, DELETE_V2)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixture_is_deterministic() {
        assert_eq!(fixture_columns(), fixture_columns());
    }

    #[test]
    fn fixture_installs_and_versions_advance() {
        let mut e = Engine::with_limits(10_000);
        install_fixture(&mut e).unwrap();
        let stats = e.table_stats(FIXTURE_TABLE).unwrap();
        assert_eq!(stats.row_count, FIXTURE_ROWS);
        assert_eq!(stats.batches, 2);
        assert_eq!(stats.current_version, 2);
        assert_eq!(stats.deleted_rows, DELETE_V1.len() + DELETE_V2.len());
        assert_eq!(stats.live_rows, FIXTURE_ROWS - 5);
        assert_eq!(stats.composite_indexes, vec![vec!["grp".to_string(), "tag".to_string()]]);
    }
}
