//! Versioned tables: typed batches + bitmap indexes + a deletion log.
//!
//! A table owns a physical row space `[0, row_count)` that only ever grows
//! (appends). Deletions are *versioned events*, not in-place mutations: each
//! delete carries a strictly increasing version, and readers ask for the
//! row universe "as of" a version. Indexes are built over the physical row
//! space and are therefore valid for every read version — the versioned
//! universe is intersected in at query time. This is what keeps the
//! composite-index mode compatible with deletions: no index ever needs to
//! know which rows are deleted.
//!
//! Failure semantics (all typed, never silent):
//! * deleting an out-of-bounds row      -> [`ErrorKind::RowIndexOutOfBounds`]
//! * deleting the same row twice        -> [`ErrorKind::RowAlreadyDeleted`]
//! * non-increasing delete version      -> [`ErrorKind::VersionOutOfOrder`]
//! * reading a version from the future  -> [`ErrorKind::ReadVersionInFuture`]
//! * duplicate composite index key      -> [`ErrorKind::IndexConflict`]

use std::sync::Arc;

use crate::batch::{BatchSchema, TypedBatch};
use crate::bits::WordBitmap;
use crate::error::{ErrorKind, IndexError, Result};
use crate::index::{CompositeIndex, ValueIndex};
use crate::scalar::Scalar;

/// One versioned deletion event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeleteEvent {
    pub version: u64,
    pub rows: Vec<usize>,
}

/// A table: schema, batches, indexes and the deletion log.
#[derive(Debug, Clone)]
pub struct Table {
    id: String,
    schema: Arc<BatchSchema>,
    batches: Vec<TypedBatch>,
    row_count: usize,
    /// One value index per column, rebuilt on append (physical row space).
    value_indexes: Vec<ValueIndex>,
    /// Explicitly registered composite indexes.
    composite_indexes: Vec<CompositeIndex>,
    /// Delete events in strictly increasing version order.
    deletes: Vec<DeleteEvent>,
    /// Cumulative deleted rows (physical space), for O(1) duplicate checks.
    deleted: WordBitmap,
    /// Highest version observed so far. Version 0 = "initial load".
    current_version: u64,
}

/// Point-in-time table statistics for introspection endpoints.
#[derive(Debug, Clone, serde::Serialize)]
pub struct TableStats {
    pub row_count: usize,
    pub live_rows: usize,
    pub deleted_rows: usize,
    pub current_version: u64,
    pub batches: usize,
    pub value_indexes: Vec<String>,
    pub composite_indexes: Vec<Vec<String>>,
}

impl Table {
    pub fn new(id: impl Into<String>, schema: Arc<BatchSchema>) -> Self {
        Self {
            id: id.into(),
            schema,
            batches: Vec::new(),
            row_count: 0,
            value_indexes: Vec::new(),
            composite_indexes: Vec::new(),
            deletes: Vec::new(),
            deleted: WordBitmap::zeros(0),
            current_version: 0,
        }
    }

    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn schema(&self) -> &Arc<BatchSchema> {
        &self.schema
    }

    pub fn row_count(&self) -> usize {
        self.row_count
    }

    pub fn current_version(&self) -> u64 {
        self.current_version
    }

    pub fn delete_log(&self) -> &[DeleteEvent] {
        &self.deletes
    }

    /// Append a typed batch. The schema must be identical to the table
    /// schema; every value index is rebuilt over the enlarged row space.
    pub fn append(&mut self, batch: TypedBatch) -> Result<()> {
        if batch.schema().as_ref() != self.schema.as_ref() {
            return Err(IndexError::new(
                ErrorKind::ArrowSchemaMismatch,
                format!(
                    "batch schema {:?} does not match table {:?} schema {:?}",
                    batch.schema().columns,
                    self.id,
                    self.schema.columns
                ),
            ));
        }
        self.row_count += batch.row_count();
        self.batches.push(batch);
        // Keep the cumulative deletion bitmap the same physical length as the
        // row space (new rows start out live).
        self.deleted = WordBitmap::from_indices(self.row_count, self.deleted.set_indices())?;
        self.rebuild_value_indexes()?;
        self.rebuild_composite_indexes()?;
        Ok(())
    }

    fn rebuild_value_indexes(&mut self) -> Result<()> {
        let mut indexes = Vec::with_capacity(self.schema.columns.len());
        for (ci, col) in self.schema.columns.iter().enumerate() {
            let cells = self.column_cells(ci)?;
            indexes.push(ValueIndex::build(&col.name, col.ty, self.row_count, &cells)?);
        }
        self.value_indexes = indexes;
        Ok(())
    }

    fn rebuild_composite_indexes(&mut self) -> Result<()> {
        let keys: Vec<Vec<String>> =
            self.composite_indexes.iter().map(|c| c.columns().to_vec()).collect();
        let mut rebuilt = Vec::with_capacity(keys.len());
        for key in keys {
            rebuilt.push(self.build_composite(&key)?);
        }
        self.composite_indexes = rebuilt;
        Ok(())
    }

    fn build_composite(&self, columns: &[String]) -> Result<CompositeIndex> {
        let mut types = Vec::with_capacity(columns.len());
        let mut cells = Vec::with_capacity(columns.len());
        for name in columns {
            let ci = self.schema.column_index(name)?;
            types.push(self.schema.columns[ci].ty);
            cells.push(self.column_cells(ci)?);
        }
        CompositeIndex::build(columns, &types, self.row_count, &cells)
    }

    /// Register a composite index over an ordered column list.
    ///
    /// Re-registering the *same* ordered key is a conflict: silently
    /// replacing or duplicating an index would hide a caller bug.
    pub fn register_composite(&mut self, columns: &[String]) -> Result<()> {
        if columns.len() < 2 {
            return Err(IndexError::new(
                ErrorKind::InvalidPredicate,
                "a composite index needs at least two columns (use the automatic value index for one)",
            ));
        }
        for name in columns {
            self.schema.column_index(name)?; // ColumnNotFound if absent
        }
        if self.composite_indexes.iter().any(|c| c.columns() == columns) {
            return Err(IndexError::new(
                ErrorKind::IndexConflict,
                format!("composite index on {columns:?} already exists on table {:?}", self.id),
            ));
        }
        let idx = self.build_composite(columns)?;
        self.composite_indexes.push(idx);
        Ok(())
    }

    /// Delete rows at `version`. Versions must be strictly increasing across
    /// calls; rows must be in bounds and not already deleted.
    pub fn delete(&mut self, version: u64, rows: &[usize]) -> Result<()> {
        if version <= self.current_version {
            return Err(IndexError::new(
                ErrorKind::VersionOutOfOrder,
                format!(
                    "delete version {version} must be greater than the current version {}",
                    self.current_version
                ),
            ));
        }
        if rows.is_empty() {
            return Err(IndexError::new(ErrorKind::InvalidPredicate, "delete event carries no rows"));
        }
        for &row in rows {
            if row >= self.row_count {
                return Err(IndexError::new(
                    ErrorKind::RowIndexOutOfBounds,
                    format!("delete: row {row} out of bounds for {} rows", self.row_count),
                ));
            }
            if self.deleted.get(row) {
                return Err(IndexError::new(
                    ErrorKind::RowAlreadyDeleted,
                    format!("delete: row {row} was already deleted at an earlier version"),
                ));
            }
        }
        let mut event_rows = rows.to_vec();
        event_rows.sort_unstable();
        event_rows.dedup();
        for &row in &event_rows {
            self.deleted.set(row)?;
        }
        self.deletes.push(DeleteEvent { version, rows: event_rows });
        self.current_version = version;
        Ok(())
    }

    /// The live-row universe as of `version` (physical length, tail-clean).
    pub fn universe_at(&self, version: u64) -> Result<WordBitmap> {
        if version > self.current_version {
            return Err(IndexError::new(
                ErrorKind::ReadVersionInFuture,
                format!(
                    "read version {version} is in the future; table {:?} is at version {}",
                    self.id, self.current_version
                ),
            ));
        }
        let mut live = WordBitmap::ones(self.row_count);
        for event in &self.deletes {
            if event.version <= version {
                let gone = WordBitmap::from_indices(self.row_count, event.rows.iter().copied())?;
                live = live.and_not(&gone)?;
            }
        }
        live.validate()?;
        Ok(live)
    }

    /// Value index for a column (always present for every schema column).
    pub fn value_index(&self, column: &str) -> Result<&ValueIndex> {
        let ci = self.schema.column_index(column)?;
        Ok(&self.value_indexes[ci])
    }

    /// Find a composite index that can serve equality conjunctions on
    /// exactly `columns` (prefix match, in predicate order).
    pub fn composite_for(&self, columns: &[&str]) -> Option<&CompositeIndex> {
        self.composite_indexes
            .iter()
            .filter(|c| c.serves(columns))
            .min_by_key(|c| c.columns().len())
    }

    /// Materialize one column across all batches (physical row order).
    pub fn column_cells(&self, col: usize) -> Result<Vec<Option<Scalar>>> {
        let mut out = Vec::with_capacity(self.row_count);
        for batch in &self.batches {
            out.extend(batch.column_cells(col)?);
        }
        Ok(out)
    }

    pub fn stats(&self) -> TableStats {
        TableStats {
            row_count: self.row_count,
            live_rows: self.row_count - self.deleted.count(),
            deleted_rows: self.deleted.count(),
            current_version: self.current_version,
            batches: self.batches.len(),
            value_indexes: self.schema.names().iter().map(|s| s.to_string()).collect(),
            composite_indexes: self.composite_indexes.iter().map(|c| c.columns().to_vec()).collect(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{ColumnSchema, ColumnType};
    use crate::scalar::CompareOp;

    fn schema() -> Arc<BatchSchema> {
        Arc::new(
            BatchSchema::new(vec![
                ColumnSchema { name: "a".into(), ty: ColumnType::Int },
                ColumnSchema { name: "b".into(), ty: ColumnType::Text },
            ])
            .unwrap(),
        )
    }

    fn batch(schema: &Arc<BatchSchema>, a: Vec<Option<i64>>, b: Vec<Option<&str>>) -> TypedBatch {
        TypedBatch::from_columns(
            schema.clone(),
            vec![
                a.into_iter().map(|v| v.map(Scalar::Int)).collect(),
                b.into_iter().map(|v| v.map(|s| Scalar::Text(s.to_string()))).collect(),
            ],
        )
        .unwrap()
    }

    #[test]
    fn universe_reflects_versioned_deletes() {
        let s = schema();
        let mut t = Table::new("t", s.clone());
        t.append(batch(&s, vec![Some(1), None, Some(3), Some(4), Some(5)], vec![None; 5])).unwrap();
        assert_eq!(t.universe_at(0).unwrap().count(), 5);

        t.delete(1, &[1, 4]).unwrap();
        assert_eq!(t.universe_at(0).unwrap().count(), 5, "version 0 predates the delete");
        let u1 = t.universe_at(1).unwrap();
        assert_eq!(u1.count(), 3);
        assert!(!u1.get(1) && !u1.get(4));

        t.delete(2, &[0]).unwrap();
        assert_eq!(t.universe_at(1).unwrap().count(), 3, "version 1 is unaffected by version 2");
        assert_eq!(t.universe_at(2).unwrap().count(), 2);
    }

    #[test]
    fn delete_failures_are_typed() {
        let s = schema();
        let mut t = Table::new("t", s.clone());
        t.append(batch(&s, vec![Some(1), Some(2)], vec![None, None])).unwrap();

        assert_eq!(t.delete(1, &[2]).unwrap_err().kind, ErrorKind::RowIndexOutOfBounds);
        t.delete(1, &[0]).unwrap();
        assert_eq!(t.delete(1, &[1]).unwrap_err().kind, ErrorKind::VersionOutOfOrder);
        assert_eq!(t.delete(0, &[1]).unwrap_err().kind, ErrorKind::VersionOutOfOrder);
        assert_eq!(t.delete(2, &[0]).unwrap_err().kind, ErrorKind::RowAlreadyDeleted);
        assert_eq!(t.universe_at(5).unwrap_err().kind, ErrorKind::ReadVersionInFuture);
    }

    #[test]
    fn indexes_track_appends_and_survive_deletes() {
        let s = schema();
        let mut t = Table::new("t", s.clone());
        t.append(batch(&s, vec![Some(7), Some(7)], vec![Some("x"), None])).unwrap();
        t.append(batch(&s, vec![Some(8)], vec![Some("x")])).unwrap();
        let idx = t.value_index("a").unwrap();
        let (tr, _) = idx.compare(CompareOp::Eq, &Scalar::Int(7)).unwrap();
        assert_eq!(tr.set_indices(), vec![0, 1]);

        t.register_composite(&["a".to_string(), "b".to_string()]).unwrap();
        assert_eq!(
            t.register_composite(&["a".to_string(), "b".to_string()]).unwrap_err().kind,
            ErrorKind::IndexConflict
        );
        assert!(t.composite_for(&["a", "b"]).is_some());
        assert!(t.composite_for(&["b"]).is_none(), "non-prefix cannot be served");

        // Deletions do not touch index contents; universes are applied at query time.
        t.delete(1, &[0]).unwrap();
        let (tr, _) = t.value_index("a").unwrap().compare(CompareOp::Eq, &Scalar::Int(7)).unwrap();
        assert_eq!(tr.set_indices(), vec![0, 1], "index still covers the physical row space");
    }

    #[test]
    fn schema_mismatch_on_append_is_typed() {
        let s = schema();
        let mut t = Table::new("t", s.clone());
        let other =
            Arc::new(BatchSchema::new(vec![ColumnSchema { name: "z".into(), ty: ColumnType::Bool }]).unwrap());
        let bad = TypedBatch::from_columns(other, vec![vec![Some(Scalar::Bool(true))]]).unwrap();
        assert_eq!(t.append(bad).unwrap_err().kind, ErrorKind::ArrowSchemaMismatch);
    }
}
