//! Resource & state layer: the typed relation catalog and synthetic fixture
//! loader.
//!
//! The catalog holds arrow2 [`Batch`]es loaded from local JSON fixtures under
//! `fixtures/tables/` (or the directory given in configuration). Per-request
//! inline relations ([`crate::api::types::QueryRequest::relations`]) overlay the
//! catalog without mutating shared state.

use crate::batch::{build_column, datum_from_json, Batch, ColType, Datum, Field};
use crate::query::{ErrorCategory, QueryError, QueryResult};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use tracing::{debug, info};

/// Shared, read-after-start relation registry.
#[derive(Debug, Clone)]
pub struct Catalog {
    relations: HashMap<String, Batch>,
    source: PathBuf,
}

impl Catalog {
    /// Load every `*.json` fixture file under `dir`. A missing directory is a
    /// startup error; an empty directory yields an empty catalog (requests can
    /// still supply inline relations).
    pub fn load_from_dir(dir: impl AsRef<Path>) -> Result<Self, LoadError> {
        let dir = dir.as_ref();
        let mut relations = HashMap::new();
        let mut files = Vec::new();
        for entry in std::fs::read_dir(dir).map_err(|e| LoadError::Io {
            dir: dir.to_path_buf(),
            source: e,
        })? {
            let entry = entry.map_err(|e| LoadError::Io { dir: dir.to_path_buf(), source: e })?;
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) == Some("json") {
                files.push(path);
            }
        }
        files.sort();
        for path in files {
            let (name, batch) = load_fixture_file(&path)?;
            if relations.insert(name.clone(), batch).is_some() {
                return Err(LoadError::Duplicate(name));
            }
            info!(relation = %name, path = %path.display(), "loaded fixture relation");
        }
        debug!(count = relations.len(), dir = %dir.display(), "catalog ready");
        Ok(Catalog { relations, source: dir.to_path_buf() })
    }

    pub fn get(&self, name: &str) -> Option<&Batch> {
        self.relations.get(name)
    }

    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.relations.keys().cloned().collect();
        v.sort();
        v
    }

    pub fn source_dir(&self) -> &Path {
        &self.source
    }
}

#[derive(Debug)]
pub enum LoadError {
    Io { dir: PathBuf, source: std::io::Error },
    Parse { path: PathBuf, source: serde_json::Error },
    BadFixture { path: PathBuf, message: String },
    Duplicate(String),
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LoadError::Io { dir, source } => {
                write!(f, "cannot read fixture directory {}: {}", dir.display(), source)
            }
            LoadError::Parse { path, source } => {
                write!(f, "invalid JSON in {}: {}", path.display(), source)
            }
            LoadError::BadFixture { path, message } => {
                write!(f, "bad fixture {}: {}", path.display(), message)
            }
            LoadError::Duplicate(name) => write!(f, "duplicate fixture relation name `{}`", name),
        }
    }
}

impl std::error::Error for LoadError {}

/// On-disk fixture envelope.
#[derive(serde::Deserialize, Debug)]
struct FixtureFile {
    name: String,
    schema: Vec<Field>,
    rows: Vec<serde_json::Map<String, serde_json::Value>>,
}

fn load_fixture_file(path: &Path) -> Result<(String, Batch), LoadError> {
    let text = std::fs::read_to_string(path).map_err(|source| LoadError::Io {
        dir: path.to_path_buf(),
        source,
    })?;
    let fixture: FixtureFile =
        serde_json::from_str(&text).map_err(|source| LoadError::Parse { path: path.to_path_buf(), source })?;
    let batch = rows_to_batch(&fixture.schema, &fixture.rows).map_err(|message| LoadError::BadFixture {
        path: path.to_path_buf(),
        message,
    })?;
    Ok((fixture.name, batch))
}

/// Validate JSON rows against the typed schema and build a columnar batch.
/// Every schema column must appear in each row (explicit `null` for SQL NULL);
/// unknown extra columns are rejected.
pub fn rows_to_batch(
    schema: &[Field],
    rows: &[serde_json::Map<String, serde_json::Value>],
) -> Result<Batch, String> {
    if schema.is_empty() {
        return Err("schema must declare at least one column".to_string());
    }
    {
        let mut seen = std::collections::HashSet::new();
        for f in schema {
            if !seen.insert(f.name.clone()) {
                return Err(format!("duplicate schema column `{}`", f.name));
            }
        }
    }
    // column -> datums
    let mut columns: Vec<Vec<Datum>> = schema.iter().map(|_| Vec::with_capacity(rows.len())).collect();
    for (row_idx, row) in rows.iter().enumerate() {
        for (col_idx, f) in schema.iter().enumerate() {
            let json = row.get(&f.name).ok_or_else(|| {
                format!("row {} missing column `{}` (use null for SQL NULL)", row_idx, f.name)
            })?;
            let datum = datum_from_json(f.col_type, json)
                .map_err(|e| format!("row {}, column `{}`: {}", row_idx, f.name, e))?;
            columns[col_idx].push(datum);
        }
        let extra: Vec<&str> = row.keys().filter(|k| !schema.iter().any(|f| f.name == **k)).map(String::as_str).collect();
        if !extra.is_empty() {
            return Err(format!("row {} has unknown column(s): {}", row_idx, extra.join(", ")));
        }
    }
    let arrays: Vec<Box<dyn arrow2::array::Array>> = schema
        .iter()
        .zip(columns)
        .map(|(f, datums)| build_column(f.col_type, &datums))
        .collect();
    Ok(Batch::new(schema.to_vec(), arrays))
}

/// Resolve a relation by name, applying an inline overlay. Overlays never touch
/// the shared [`Catalog`].
pub fn resolve_relation<'a>(
    catalog: &'a Catalog,
    overlays: &'a HashMap<String, Batch>,
    name: &str,
) -> QueryResult<&'a Batch> {
    if let Some(b) = overlays.get(name) {
        return Ok(b);
    }
    catalog.get(name).ok_or_else(|| {
        QueryError::new(
            ErrorCategory::Validation,
            "unknown_relation",
            format!(
                "relation `{}` not found (inline: {}; catalog: {})",
                name,
                overlay_names(overlays),
                catalog.names().join(", ")
            ),
            "validate",
        )
    })
}

fn overlay_names(overlays: &HashMap<String, Batch>) -> String {
    if overlays.is_empty() {
        return "<none>".to_string();
    }
    let mut v: Vec<String> = overlays.keys().cloned().collect();
    v.sort();
    v.join(", ")
}

/// Type used by API responses to describe catalog contents.
pub fn describe_type(t: ColType) -> &'static str {
    match t {
        ColType::Int64 => "Int64",
        ColType::Float64 => "Float64",
        ColType::Boolean => "Boolean",
        ColType::Utf8 => "Utf8",
    }
}
