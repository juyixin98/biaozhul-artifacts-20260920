//! Shared service state: the immutable relation catalog, per-query prepared
//! joins and configuration.

use std::collections::BTreeMap;
use std::sync::{Arc, RwLock};

use crate::batch::RecordBatch;
use crate::config::Config;
use crate::dataset::FixtureSet;
use crate::error::{ErrorCode, JoinError, Result};
use crate::executor::PreparedJoin;
use crate::plan::JoinRequest;

/// Process-wide shared application state.
#[derive(Clone)]
pub struct AppState {
    inner: Arc<StateInner>,
}

struct StateInner {
    config: Config,
    /// Immutable catalog of named typed relations.
    catalog: RwLock<BTreeMap<String, RecordBatch>>,
    /// Prepared (planned + trie-built) joins keyed by a canonical query key.
    prepared: RwLock<BTreeMap<String, Arc<PreparedJoin>>>,
}

impl AppState {
    /// Build state from a named fixture set.
    pub fn from_fixture(config: Config, fixture: FixtureSet) -> Self {
        AppState {
            inner: Arc::new(StateInner {
                config,
                catalog: RwLock::new(fixture.relations),
                prepared: RwLock::new(BTreeMap::new()),
            }),
        }
    }

    pub fn config(&self) -> &Config {
        &self.inner.config
    }

    /// Snapshot the catalog (cheap clone of the typed batches; fixtures are
    /// small). Held only for the duration of request handling.
    pub fn catalog_snapshot(&self) -> Result<BTreeMap<String, RecordBatch>> {
        self.inner
            .catalog
            .read()
            .map(|c| c.clone())
            .map_err(|_| JoinError::new(ErrorCode::Internal, "catalog lock poisoned"))
    }

    /// Add or replace a relation (kept for embedders/tests; the HTTP surface
    /// serves the immutable fixture catalog only).
    pub fn put_relation(&self, name: impl Into<String>, batch: RecordBatch) -> Result<()> {
        let mut catalog = self
            .inner
            .catalog
            .write()
            .map_err(|_| JoinError::new(ErrorCode::Internal, "catalog lock poisoned"))?;
        catalog.insert(name.into(), batch);
        // Any prepared join referencing changed data is invalidated.
        if let Ok(mut p) = self.inner.prepared.write() {
            p.clear();
        }
        Ok(())
    }

    /// Get or build a prepared join for a request, returning a shared handle.
    pub fn prepare(&self, request: &JoinRequest) -> Result<Arc<PreparedJoin>> {
        let key = canonical_key(request);
        if let Some(found) = self
            .inner
            .prepared
            .read()
            .ok()
            .and_then(|p| p.get(&key).cloned())
        {
            return Ok(found);
        }
        let catalog = self.catalog_snapshot()?;
        let prepared = Arc::new(PreparedJoin::prepare(request, &catalog)?);
        let mut guard = self
            .inner
            .prepared
            .write()
            .map_err(|_| JoinError::new(ErrorCode::Internal, "prepared lock poisoned"))?;
        // Another request may have built it concurrently; prefer that one.
        Ok(guard.entry(key).or_insert(prepared).clone())
    }

    /// Diagnostic: number of cached prepared joins.
    pub fn prepared_count(&self) -> usize {
        self.inner
            .prepared
            .read()
            .map(|p| p.len())
            .unwrap_or(0)
    }

    /// Diagnostic: relation names and row counts (no data content).
    pub fn relation_summary(&self) -> Result<BTreeMap<String, usize>> {
        let catalog = self
            .inner
            .catalog
            .read()
            .map_err(|_| JoinError::new(ErrorCode::Internal, "catalog lock poisoned"))?;
        Ok(catalog.iter().map(|(k, v)| (k.clone(), v.n_rows())).collect())
    }
}

/// Deterministic key for caching identical requests.
fn canonical_key(r: &JoinRequest) -> String {
    let rels: Vec<String> = r
        .relations
        .iter()
        .map(|o| format!("{}({})", o.relation, o.variables.join(",")))
        .collect();
    let proj = r
        .projection
        .as_ref()
        .map(|p| p.join(","))
        .unwrap_or_else(|| "*".into());
    format!(
        "{}|proj={}|null={}",
        rels.join(";"),
        proj,
        r.null_policy.as_str()
    )
}
