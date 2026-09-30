//! Resources and shared state: in-memory table catalog loaded from local
//! synthetic JSON fixtures (and optional per-request inline tables).
//!
//! Fixture file layout: `<fixtures_dir>/<table_name>.json`
//! ```json
//! {
//!   "columns": [
//!     {"name": "id",    "type": "int",    "values": [1, 2, null]},
//!     {"name": "label", "type": "str",    "values": ["a", null, "b"]},
//!     {"name": "ratio", "type": "float",  "values": [0.5, null, 1.25]},
//!     {"name": "flag",  "type": "bool",   "values": [true, false, null]}
//!   ]
//! }
//! ```

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use serde::Deserialize;

use crate::batch::{Batch, ColumnBuilder, ScalarType};
use crate::error::{EngineError, EngineResult, ErrorKind};

#[derive(Debug, Clone)]
pub struct Table {
    pub name: String,
    pub batch: Batch,
}

impl Table {
    pub fn column_type(&self, col: &str) -> EngineResult<ScalarType> {
        Ok(self.batch.column(col)?.ty)
    }
}

/// Process-wide immutable catalog. Wrapped in `Arc` and shared across handlers.
#[derive(Debug, Clone, Default)]
pub struct Catalog {
    tables: HashMap<String, Arc<Table>>,
    max_rows_per_table: usize,
}

impl Catalog {
    pub fn from_fixtures(dir: &Path, max_rows_per_table: usize) -> EngineResult<Self> {
        let mut catalog = Catalog {
            tables: HashMap::new(),
            max_rows_per_table,
        };
        if !dir.exists() {
            // Empty fixtures dir is legal: request bodies can supply inline tables.
            tracing::warn!(dir = %dir.display(), "fixtures directory does not exist; starting with empty catalog");
            return Ok(catalog);
        }
        let mut entries: Vec<_> = std::fs::read_dir(dir)
            .map_err(|e| EngineError::internal(format!("read fixtures dir: {e}")))?
            .filter_map(|e| e.ok())
            .collect();
        entries.sort_by_key(|e| e.path());

        for entry in entries {
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            let name = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or_default()
                .to_string();
            let text = std::fs::read_to_string(&path)
                .map_err(|e| EngineError::internal(format!("read {}: {e}", path.display())))?;
            let json: FixtureFile = serde_json::from_str(&text).map_err(|e| {
                EngineError::new(
                    ErrorKind::Validation,
                    format!("invalid fixture {}: {e}", path.display()),
                )
            })?;
            let table = build_table(&name, json.columns, max_rows_per_table)?;
            tracing::debug!(table = %name, rows = table.batch.n_rows, "loaded fixture");
            catalog.tables.insert(name, Arc::new(table));
        }
        Ok(catalog)
    }

    pub fn get(&self, name: &str) -> EngineResult<Arc<Table>> {
        self.tables.get(name).cloned().ok_or_else(|| {
            EngineError::validation(format!(
                "unknown table '{name}' (loaded: {})",
                self.table_names().join(", ")
            ))
        })
    }

    pub fn table_names(&self) -> Vec<String> {
        let mut v: Vec<_> = self.tables.keys().cloned().collect();
        v.sort();
        v
    }

    /// Layer inline, per-request tables on top of the base catalog. The
    /// returned [`Catalog`] is scoped to one request; the base catalog is
    /// never mutated (immutability).
    pub fn with_inline(&self, inline: Vec<FixtureTable>) -> EngineResult<Self> {
        let mut merged = self.clone();
        for t in inline {
            let table = build_table(&t.name, t.columns, self.max_rows_per_table)?;
            merged.tables.insert(t.name, Arc::new(table));
        }
        Ok(merged)
    }
}

// ---------- fixture JSON structures ----------

#[derive(Debug, Clone, Deserialize)]
pub struct FixtureFile {
    pub columns: Vec<FixtureColumn>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FixtureTable {
    pub name: String,
    pub columns: Vec<FixtureColumn>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FixtureColumn {
    pub name: String,
    #[serde(rename = "type")]
    pub ty_name: String,
    pub values: Vec<Option<serde_json::Value>>,
}

fn parse_type(name: &str) -> EngineResult<ScalarType> {
    match name {
        "bool" | "boolean" => Ok(ScalarType::Bool),
        "int" | "integer" | "bigint" => Ok(ScalarType::Int),
        "float" | "double" | "f64" => Ok(ScalarType::Float),
        "str" | "string" | "text" | "utf8" => Ok(ScalarType::Str),
        other => Err(EngineError::validation(format!(
            "unsupported fixture type '{other}' (want bool|int|float|str)"
        ))),
    }
}

fn build_table(
    name: &str,
    columns: Vec<FixtureColumn>,
    max_rows: usize,
) -> EngineResult<Table> {
    if columns.is_empty() {
        return Err(EngineError::validation(format!(
            "table '{name}' must have at least one column"
        )));
    }
    let n_rows = columns[0].values.len();
    if n_rows > max_rows {
        return Err(EngineError::new(
            ErrorKind::LimitExceeded,
            format!("table '{name}' has {n_rows} rows, limit is {max_rows}"),
        ));
    }
    let mut built = Vec::with_capacity(columns.len());
    for fc in &columns {
        if fc.values.len() != n_rows {
            return Err(EngineError::validation(format!(
                "column '{}' has {} values but table has {n_rows}",
                fc.name,
                fc.values.len()
            )));
        }
        let ty = parse_type(&fc.ty_name)?;
        built.push(match ty {
            ScalarType::Bool => {
                let vals: EngineResult<Vec<Option<bool>>> = fc
                    .values
                    .iter()
                    .map(|v| match v {
                        None | Some(serde_json::Value::Null) => Ok(None),
                        Some(serde_json::Value::Bool(b)) => Ok(Some(*b)),
                        Some(other) => Err(type_error(name, &fc.name, "bool", other)),
                    })
                    .collect();
                ColumnBuilder::bools(&fc.name, &vals?)
            }
            ScalarType::Int => {
                let vals: EngineResult<Vec<Option<i64>>> = fc
                    .values
                    .iter()
                    .map(|v| match v {
                        None | Some(serde_json::Value::Null) => Ok(None),
                        Some(serde_json::Value::Number(n)) => match n.as_i64() {
                            Some(i) => Ok(Some(i)),
                            None => Err(type_error(name, &fc.name, "int", v.as_ref().unwrap())),
                        },
                        Some(other) => Err(type_error(name, &fc.name, "int", other)),
                    })
                    .collect();
                ColumnBuilder::ints(&fc.name, &vals?)
            }
            ScalarType::Float => {
                let vals: EngineResult<Vec<Option<f64>>> = fc
                    .values
                    .iter()
                    .map(|v| match v {
                        None | Some(serde_json::Value::Null) => Ok(None),
                        // ints are accepted and widened to float
                        Some(serde_json::Value::Number(n)) => n
                            .as_f64()
                            .map(Some)
                            .ok_or_else(|| type_error(name, &fc.name, "float", v.as_ref().unwrap())),
                        Some(other) => Err(type_error(name, &fc.name, "float", other)),
                    })
                    .collect();
                ColumnBuilder::floats(&fc.name, &vals?)
            }
            ScalarType::Str => {
                let vals: EngineResult<Vec<Option<&str>>> = fc
                    .values
                    .iter()
                    .map(|v| match v {
                        None | Some(serde_json::Value::Null) => Ok(None),
                        Some(serde_json::Value::String(s)) => Ok(Some(s.as_str())),
                        Some(other) => Err(type_error(name, &fc.name, "str", other)),
                    })
                    .collect();
                // Borrowed strings escape the closure; rebuild as owned below.
                let owned: Vec<Option<String>> = vals?.into_iter().map(|o| o.map(str::to_string)).collect();
                let refs: Vec<Option<&str>> = owned.iter().map(|o| o.as_deref()).collect();
                ColumnBuilder::strs(&fc.name, &refs)
            }
        });
    }
    Ok(Table {
        name: name.to_string(),
        batch: Batch::new(built)?,
    })
}

fn type_error(table: &str, col: &str, want: &str, got: &serde_json::Value) -> EngineError {
    EngineError::validation(format!(
        "table '{table}', column '{col}': expected {want}, got JSON {got}"
    ))
}
