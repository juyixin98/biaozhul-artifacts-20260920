//! In-memory catalog, server configuration and load control.
//!
//! The catalog is the only mutable server state. Relations are stored tries
//! built from validated typed batches; every successful mutation bumps an
//! opaque monotonic version, which is what resumable cursors pin themselves
//! to. Configuration is read once at startup (env + CLI) and exposed through
//! [`ServerConfig`].

use std::sync::{Arc, RwLock};

use serde::Serialize;

use crate::error::{Error, ReasonCode, Result};
use crate::query::batch::{IngestReport, TypedBatch};
use crate::query::model::{PlannerConfig, SchemaSource};
use crate::query::trie::Trie;
use crate::query::value::Scalar;

mod config;

pub use config::ServerConfig;

/// One stored relation: its declared column order/types, multiset trie and
/// ingestion metadata.
#[derive(Debug, Clone)]
pub struct Relation {
    pub columns: Vec<(String, crate::query::batch::ColumnType)>,
    pub trie: Trie,
    pub rows_loaded: u64,
    pub distinct_rows: usize,
}

impl Relation {
    fn from_report(report: &IngestReport) -> Self {
        let batch: &TypedBatch = &report.batch;
        let trie = Trie::build(batch.rows.clone());
        Self {
            columns: batch.columns.clone(),
            rows_loaded: trie.total_rows(),
            distinct_rows: trie.distinct_rows(),
            trie,
        }
    }
}

/// Snapshot read for query planning.
#[derive(Debug, Clone)]
pub struct CatalogSnapshot {
    version: u64,
    relations: std::collections::BTreeMap<String, Relation>,
}

impl SchemaSource for CatalogSnapshot {
    fn arity(&self, relation: &str) -> Option<usize> {
        self.relations.get(relation).map(|r| r.trie.arity())
    }

    fn multiset_rows(&self, relation: &str) -> Option<Vec<(Vec<Scalar>, u64)>> {
        self.relations
            .get(relation)
            .map(|r| r.trie.distinct_leaves())
    }

    fn version(&self) -> u64 {
        self.version
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadOutcome {
    pub relation: String,
    pub version: u64,
    pub rows_loaded: u64,
    pub distinct_rows: usize,
    pub dropped_null_rows: usize,
    pub replaced: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RelationInfo {
    pub name: String,
    pub arity: usize,
    pub columns: Vec<String>,
    pub rows_loaded: u64,
    pub distinct_rows: usize,
}

/// Thread-safe in-memory catalog.
#[derive(Debug, Clone)]
pub struct Catalog {
    inner: Arc<RwLock<CatalogInner>>,
}

#[derive(Debug)]
struct CatalogInner {
    version: u64,
    relations: std::collections::BTreeMap<String, Relation>,
}

impl Default for Catalog {
    fn default() -> Self {
        Self::new()
    }
}

impl Catalog {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(CatalogInner {
                version: 1,
                relations: std::collections::BTreeMap::new(),
            })),
        }
    }

    /// Immutable planning snapshot (cheap: clones the small metadata and
    /// reference-counted trie data structures; view building copies rows).
    pub fn snapshot(&self) -> CatalogSnapshot {
        let guard = self.inner.read().expect("catalog lock poisoned");
        CatalogSnapshot {
            version: guard.version,
            relations: guard.relations.clone(),
        }
    }

    pub fn current_version(&self) -> u64 {
        self.inner.read().expect("catalog lock poisoned").version
    }

    pub fn list(&self) -> Vec<RelationInfo> {
        let guard = self.inner.read().expect("catalog lock poisoned");
        guard
            .relations
            .iter()
            .map(|(name, r)| RelationInfo {
                name: name.clone(),
                arity: r.trie.arity(),
                columns: r.columns.iter().map(|(c, _)| c.clone()).collect(),
                rows_loaded: r.rows_loaded,
                distinct_rows: r.distinct_rows,
            })
            .collect()
    }

    pub fn get(&self, name: &str) -> Option<RelationInfo> {
        let guard = self.inner.read().expect("catalog lock poisoned");
        guard.relations.get(name).map(|r| RelationInfo {
            name: name.to_string(),
            arity: r.trie.arity(),
            columns: r.columns.iter().map(|(c, _)| c.clone()).collect(),
            rows_loaded: r.rows_loaded,
            distinct_rows: r.distinct_rows,
        })
    }

    /// Validate a relation name: non-empty, lowercase/uppercase letters,
    /// digits and `_`, must start with a letter or underscore.
    pub fn validate_name(name: &str) -> Result<()> {
        let valid = !name.is_empty()
            && name.len() <= 128
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_')
            && name
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic() || c == '_');
        if valid {
            Ok(())
        } else {
            Err(Error::validation(
                ReasonCode::InvalidRelationName,
                "relation name must match [A-Za-z_][A-Za-z0-9_]{0,127}",
            ))
        }
    }

    /// Install a validated batch, honoring `replace` and bumping the version.
    pub fn load(&self, name: &str, report: IngestReport) -> Result<LoadOutcome> {
        Self::validate_name(name)?;
        let replace = report.replace;
        let mut guard = self.inner.write().expect("catalog lock poisoned");
        let existed = guard.relations.contains_key(name);
        if existed && !replace {
            return Err(Error::validation(
                ReasonCode::RelationExists,
                format!(
                    "relation {name} already exists; resend with \"replace\": true \
                     to overwrite it"
                ),
            ));
        }
        let relation = Relation::from_report(&report);
        let rows_loaded = relation.rows_loaded;
        let distinct_rows = relation.distinct_rows;
        guard.relations.insert(name.to_string(), relation);
        guard.version += 1;
        Ok(LoadOutcome {
            relation: name.to_string(),
            version: guard.version,
            rows_loaded,
            distinct_rows,
            dropped_null_rows: report.dropped_null_rows,
            replaced: existed,
        })
    }

    pub fn planner_config(cfg: &ServerConfig) -> PlannerConfig {
        PlannerConfig {
            max_variables: cfg.max_variables,
            allow_reindex: cfg.allow_reindex,
        }
    }
}
