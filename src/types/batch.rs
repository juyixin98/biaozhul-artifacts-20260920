//! Typed, in-memory columnar batches.
//!
//! A [`Batch`] is the unit of data exchanged between modules. Every column is
//! strongly typed; the join engine currently supports 64-bit signed integer
//! join keys. Rows are identified by their **stable positional index** in the
//! batch — two rows with identical values are still distinct identities, which
//! is what lets the engine preserve join-result multiplicity for duplicates.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use crate::error::Error;

/// Physical data types supported by the typed batch layer.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DataType {
    /// 64-bit signed integer. The only type currently supported on join keys.
    Int64,
}

impl DataType {
    pub fn as_str(self) -> &'static str {
        match self {
            DataType::Int64 => "int64",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "int64" | "i64" => Some(DataType::Int64),
            _ => None,
        }
    }
}

/// One nullable column.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    pub data_type: DataType,
    /// Length equals the batch row count; `None` encodes SQL NULL.
    pub values: Vec<Option<i64>>,
}

impl Column {
    pub fn new(name: impl Into<String>, values: Vec<Option<i64>>) -> Self {
        Self {
            name: name.into(),
            data_type: DataType::Int64,
            values,
        }
    }
}

/// A typed columnar batch: an ordered collection of same-length columns.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Batch {
    pub columns: Vec<Column>,
}

impl Batch {
    pub fn new(columns: Vec<Column>) -> Result<Self, Error> {
        let batch = Self { columns };
        batch.validate()?;
        Ok(batch)
    }

    pub fn validate(&self) -> Result<(), Error> {
        if self.columns.is_empty() {
            return Err(Error::input("empty_batch", "batch must contain at least one column"));
        }
        let row_count = self.columns[0].values.len();
        for c in &self.columns {
            if c.name.is_empty() {
                return Err(Error::input("empty_column_name", "column names must be non-empty"));
            }
            if c.values.len() != row_count {
                return Err(Error::input(
                    "ragged_batch",
                    format!(
                        "column '{}' has {} rows, expected {}",
                        c.name,
                        c.values.len(),
                        row_count
                    ),
                ));
            }
        }
        let mut seen = HashMap::new();
        for c in &self.columns {
            if let Some(_prev) = seen.insert(c.name.clone(), ()) {
                return Err(Error::input(
                    "duplicate_column",
                    format!("duplicate column name '{}'", c.name),
                ));
            }
        }
        Ok(())
    }

    pub fn row_count(&self) -> usize {
        self.columns.first().map(|c| c.values.len()).unwrap_or(0)
    }

    pub fn column_index(&self, name: &str) -> Result<usize, Error> {
        self.columns
            .iter()
            .position(|c| c.name == name)
            .ok_or_else(|| {
                Error::input(
                    "unknown_column",
                    format!("no column named '{name}' in batch"),
                )
            })
    }

    pub fn column(&self, name: &str) -> Result<&Column, Error> {
        let idx = self.column_index(name)?;
        Ok(&self.columns[idx])
    }

    /// Value at (`column`, `row`); `None` means SQL NULL.
    pub fn get(&self, column: &str, row: usize) -> Result<Option<i64>, Error> {
        Ok(self.column(column)?.values[row])
    }

    pub fn column_names(&self) -> Vec<String> {
        self.columns.iter().map(|c| c.name.clone()).collect()
    }
}

/// A named relation: a table alias plus one typed batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Relation {
    pub name: String,
    pub batch: Batch,
}

impl Relation {
    pub fn new(name: impl Into<String>, batch: Batch) -> Self {
        Self {
            name: name.into(),
            batch,
        }
    }
}
