//! Dataset resource & state: an immutable typed data snapshot, its column
//! bitmap indexes, and the append-only deletion log.
//!
//! ## Versioning model
//!
//! * Data (rows) is loaded once at snapshot version `data_version` and is
//!   immutable; every column index is stamped with that same version.
//! * Deletions advance a monotonic table version. They do not mutate data or
//!   indexes — they change the **live domain** a query is evaluated against.
//! * Queries may run at the current version or an explicitly pinned one.
//!
//! ## Index compatibility
//!
//! Combining predicates whose indexes came from different snapshots (different
//! row space / domain) is rejected with
//! [`TritmapError::IncompatibleIndexVersion`] rather than silently evaluated on
//! mismatched bitmaps.

use std::collections::HashMap;

use crate::batch::{ColumnSpec, TypedBatch};
use crate::bitset::Bitmap;
use crate::deletion::DeletionLog;
use crate::error::{Result, TritmapError};
use crate::index::ColumnIndex;

#[derive(Debug, Clone)]
pub struct Dataset {
    name: String,
    batch: TypedBatch,
    indexes: HashMap<String, ColumnIndex>,
    deletions: DeletionLog,
    /// Snapshot version the data/indexes were built at.
    data_version: u64,
}

impl Dataset {
    /// Load a batch at `data_version` and build indexes for the given columns
    /// (all columns when `index_columns` is empty).
    pub fn load(
        name: impl Into<String>,
        batch: TypedBatch,
        data_version: u64,
        index_columns: &[String],
    ) -> Result<Self> {
        let name = name.into();
        let cols: Vec<String> = if index_columns.is_empty() {
            batch.columns().iter().map(|c| c.name.clone()).collect()
        } else {
            index_columns.to_vec()
        };
        let mut indexes = HashMap::new();
        for c in &cols {
            if batch.column_index(c).is_none() {
                return Err(TritmapError::UnknownColumn(c.clone()));
            }
            indexes.insert(c.clone(), ColumnIndex::build(&batch, c, data_version)?);
        }
        Ok(Self {
            name,
            deletions: DeletionLog::new(batch.len(), data_version),
            batch,
            indexes,
            data_version,
        })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn row_count(&self) -> usize {
        self.batch.len()
    }

    pub fn columns(&self) -> &[ColumnSpec] {
        self.batch.columns()
    }

    pub fn batch(&self) -> &TypedBatch {
        &self.batch
    }

    pub fn data_version(&self) -> u64 {
        self.data_version
    }

    pub fn current_version(&self) -> u64 {
        self.deletions.current_version()
    }

    pub fn deletions(&self) -> &DeletionLog {
        &self.deletions
    }

    pub fn index(&self, column: &str) -> Result<&ColumnIndex> {
        self.indexes
            .get(column)
            .ok_or_else(|| TritmapError::UnknownColumn(column.to_string()))
    }

    pub fn indexed_columns(&self) -> Vec<&str> {
        let mut v: Vec<&str> = self.indexes.keys().map(String::as_str).collect();
        v.sort();
        v
    }

    /// Live domain at a given version. Versions before the data snapshot are
    /// rejected (nothing to query); future versions clamp to current.
    pub fn domain_at(&self, version: Option<u64>) -> Result<(u64, Bitmap)> {
        let v = version.unwrap_or_else(|| self.deletions.current_version());
        if v < self.data_version {
            return Err(TritmapError::VersionOutOfOrder {
                current: self.data_version,
                got: v,
            });
        }
        Ok((v, self.deletions.alive_at(v)))
    }

    /// Record a deletion; delegates categorical validation to [`DeletionLog`].
    pub fn delete_row(&mut self, row: usize, version: u64) -> Result<()> {
        if row >= self.row_count() {
            return Err(TritmapError::RowNotFound {
                table: self.name.clone(),
                row,
                n: self.row_count(),
            });
        }
        self.deletions.delete(row, version)
    }

    /// Assert that two indexed columns can be combined in one query: same
    /// snapshot version and equal row space. Returns the shared data version.
    pub fn assert_compatible(&self, left: &str, right: &str, op: &str) -> Result<u64> {
        let li = self.index(left)?;
        let ri = self.index(right)?;
        if li.built_at() != ri.built_at() || li.row_space_len() != ri.row_space_len() {
            return Err(TritmapError::IncompatibleIndexVersion {
                left_col: left.to_string(),
                left_version: li.built_at(),
                left_domain: li.row_space_len(),
                right_col: right.to_string(),
                right_version: ri.built_at(),
                right_domain: ri.row_space_len(),
                op: op.to_string(),
            });
        }
        Ok(li.built_at())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::BatchBuilder;

    fn ds() -> Dataset {
        let b = BatchBuilder::new()
            .add_integer("a", vec![Some(1), None, Some(3)])
            .add_text("b", vec![Some("x"), Some("y"), None])
            .build()
            .unwrap();
        Dataset::load("t", b, 1, &[]).unwrap()
    }

    #[test]
    fn builds_all_indexes_and_tracks_versions() {
        let mut d = ds();
        assert_eq!(d.indexed_columns(), vec!["a", "b"]);
        assert_eq!(d.current_version(), 1);
        d.delete_row(1, 2).unwrap();
        assert_eq!(d.current_version(), 2);
        let (v, dom) = d.domain_at(None).unwrap();
        assert_eq!(v, 2);
        assert_eq!(dom.count_ones(), 2);
        // Historical domain at v1 still has all rows.
        assert_eq!(d.domain_at(Some(1)).unwrap().1.count_ones(), 3);
    }

    #[test]
    fn pre_snapshot_version_is_rejected() {
        let d = ds();
        assert!(matches!(
            d.domain_at(Some(0)),
            Err(TritmapError::VersionOutOfOrder { .. })
        ));
    }

    #[test]
    fn unknown_column_and_compatibility() {
        let d = ds();
        assert!(matches!(d.index("nope"), Err(TritmapError::UnknownColumn(_))));
        assert_eq!(d.assert_compatible("a", "b", "AND").unwrap(), 1);
    }
}
