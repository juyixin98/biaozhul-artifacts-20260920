//! Engine: catalog and shared resource state behind every entry point.
//!
//! The engine owns all tables, enforces the configured resource limits and
//! assigns every query a unique run id so logs, HTTP responses and test
//! output can be correlated back to their input. It is the single stateful
//! object the HTTP layer and the CLI demo share.

use std::collections::HashMap;

use serde::Serialize;
use uuid::Uuid;

use crate::batch::BatchSchema;
use crate::error::{ErrorKind, IndexError, Result};
use crate::eval::{evaluate, EvalStep, Strategy};
use crate::predicate::Predicate;
use crate::scalar::Scalar;
use crate::table::{Table, TableStats};
use crate::trivalent::TriCounts;

/// Catalog + limits. Construct via [`Engine::with_limits`].
#[derive(Debug)]
pub struct Engine {
    tables: HashMap<String, Table>,
    max_rows_per_table: usize,
}

/// Full, serializable outcome of one query (returned by the HTTP API too).
#[derive(Debug, Clone, Serialize)]
pub struct QueryOutcome {
    pub run_id: String,
    pub table: String,
    pub read_version: u64,
    pub strategy: String,
    pub predicate: serde_json::Value,
    pub true_rows: Vec<usize>,
    pub false_rows: Vec<usize>,
    pub unknown_rows: Vec<usize>,
    pub counts: OutcomeCounts,
    pub steps: Vec<EvalStep>,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct OutcomeCounts {
    pub universe_rows: usize,
    pub true_rows: usize,
    pub false_rows: usize,
    pub unknown_rows: usize,
}

impl From<(usize, TriCounts)> for OutcomeCounts {
    fn from((universe, c): (usize, TriCounts)) -> Self {
        Self {
            universe_rows: universe,
            true_rows: c.true_rows,
            false_rows: c.false_rows,
            unknown_rows: c.unknown_rows,
        }
    }
}

impl Engine {
    pub fn with_limits(max_rows_per_table: usize) -> Self {
        Self { tables: HashMap::new(), max_rows_per_table }
    }

    pub fn create_table(&mut self, id: &str, schema: BatchSchema) -> Result<()> {
        if self.tables.contains_key(id) {
            return Err(IndexError::new(
                ErrorKind::TableExists,
                format!("table {id:?} already exists"),
            ));
        }
        self.tables.insert(id.to_string(), Table::new(id, std::sync::Arc::new(schema)));
        tracing::info!(table = %id, "table created");
        Ok(())
    }

    pub fn table(&self, id: &str) -> Result<&Table> {
        self.tables
            .get(id)
            .ok_or_else(|| IndexError::new(ErrorKind::TableNotFound, format!("no table named {id:?}")))
    }

    fn table_mut(&mut self, id: &str) -> Result<&mut Table> {
        self.tables
            .get_mut(id)
            .ok_or_else(|| IndexError::new(ErrorKind::TableNotFound, format!("no table named {id:?}")))
    }

    /// Append one typed batch given as column-major cells.
    pub fn append_columns(&mut self, table: &str, columns: Vec<Vec<Option<Scalar>>>) -> Result<usize> {
        let batch = {
            let t = self.table(table)?;
            crate::batch::TypedBatch::from_columns(t.schema().clone(), columns)?
        };
        let new_total = self.table(table)?.row_count() + batch.row_count();
        if new_total > self.max_rows_per_table {
            return Err(IndexError::new(
                ErrorKind::ResourceLimit,
                format!(
                    "append would grow table {table:?} to {new_total} rows, limit is {}",
                    self.max_rows_per_table
                ),
            ));
        }
        let added = batch.row_count();
        self.table_mut(table)?.append(batch)?;
        tracing::info!(table = %table, rows = added, total = new_total, "batch appended");
        Ok(added)
    }

    pub fn delete_rows(&mut self, table: &str, version: u64, rows: &[usize]) -> Result<()> {
        let t = self.table_mut(table)?;
        t.delete(version, rows)?;
        tracing::info!(table = %table, version = version, deleted = rows.len(), "rows deleted");
        Ok(())
    }

    pub fn register_composite(&mut self, table: &str, columns: &[String]) -> Result<()> {
        let t = self.table_mut(table)?;
        t.register_composite(columns)?;
        tracing::info!(table = %table, columns = ?columns, "composite index registered");
        Ok(())
    }

    /// Run a query. `version == None` reads the latest known version.
    pub fn query(
        &self,
        table: &str,
        pred: &Predicate,
        strategy: Strategy,
        version: Option<u64>,
    ) -> Result<QueryOutcome> {
        let t = self.table(table)?;
        let read_version = version.unwrap_or_else(|| t.current_version());
        let run_id = Uuid::new_v4().to_string();
        tracing::info!(
            run_id = %run_id,
            table = %table,
            read_version = read_version,
            strategy = %strategy.as_str(),
            predicate = %pred.describe(),
            "query start"
        );
        let out = evaluate(t, pred, strategy, read_version, &run_id)?;
        let counts = out.partition.counts();
        let universe_rows = out.partition.universe().count();
        let outcome = QueryOutcome {
            run_id: run_id.clone(),
            table: table.to_string(),
            read_version,
            strategy: strategy.as_str().to_string(),
            predicate: pred.to_json(),
            true_rows: out.partition.true_set().set_indices(),
            false_rows: out.partition.false_set().set_indices(),
            unknown_rows: out.partition.unknown_set().set_indices(),
            counts: OutcomeCounts::from((universe_rows, counts)),
            steps: out.steps,
        };
        tracing::info!(
            run_id = %run_id,
            universe_rows = outcome.counts.universe_rows,
            true_rows = outcome.counts.true_rows,
            false_rows = outcome.counts.false_rows,
            unknown_rows = outcome.counts.unknown_rows,
            "query done"
        );
        Ok(outcome)
    }

    pub fn table_stats(&self, id: &str) -> Result<TableStats> {
        Ok(self.table(id)?.stats())
    }

    pub fn list_tables(&self) -> Vec<(String, TableStats)> {
        let mut out: Vec<_> = self.tables.iter().map(|(k, v)| (k.clone(), v.stats())).collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{ColumnSchema, ColumnType};
    use crate::scalar::CompareOp;

    fn engine_with_table() -> Engine {
        let mut e = Engine::with_limits(1000);
        e.create_table(
            "t",
            BatchSchema::new(vec![ColumnSchema { name: "a".into(), ty: ColumnType::Int }]).unwrap(),
        )
        .unwrap();
        e.append_columns("t", vec![vec![Some(Scalar::Int(1)), None, Some(Scalar::Int(2))]]).unwrap();
        e
    }

    #[test]
    fn duplicate_table_is_typed_error() {
        let mut e = engine_with_table();
        let err = e
            .create_table(
                "t",
                BatchSchema::new(vec![ColumnSchema { name: "a".into(), ty: ColumnType::Int }]).unwrap(),
            )
            .unwrap_err();
        assert_eq!(err.kind, ErrorKind::TableExists);
        assert_eq!(e.table("nope").unwrap_err().kind, ErrorKind::TableNotFound);
    }

    #[test]
    fn row_limit_is_enforced() {
        let mut e = Engine::with_limits(2);
        e.create_table(
            "t",
            BatchSchema::new(vec![ColumnSchema { name: "a".into(), ty: ColumnType::Int }]).unwrap(),
        )
        .unwrap();
        e.append_columns("t", vec![vec![Some(Scalar::Int(1)), Some(Scalar::Int(2))]]).unwrap();
        let err = e.append_columns("t", vec![vec![Some(Scalar::Int(3))]]).unwrap_err();
        assert_eq!(err.kind, ErrorKind::ResourceLimit);
    }

    #[test]
    fn query_outcome_carries_run_identity_and_steps() {
        let e = engine_with_table();
        let p = Predicate::not(Predicate::compare("a", CompareOp::Eq, Scalar::Int(1)));
        let out = e.query("t", &p, Strategy::Single, None).unwrap();
        assert!(!out.run_id.is_empty());
        assert_eq!(out.read_version, 0);
        assert_eq!(out.counts.universe_rows, 3);
        // NOT (a = 1): row0 F, row1 U (NULL), row2 T
        assert_eq!(out.true_rows, vec![2]);
        assert_eq!(out.false_rows, vec![0]);
        assert_eq!(out.unknown_rows, vec![1]);
        assert!(out.steps.iter().all(|s| s.run_id == out.run_id));
        assert!(out.steps.iter().any(|s| s.op == "kleene_not"));
    }
}
