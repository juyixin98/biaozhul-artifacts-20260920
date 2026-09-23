//! Application service: serialises writers, exposes typed operations and
//! builds proofs from immutable version snapshots.

use std::sync::Arc;

use tokio::sync::Mutex;

use crate::error::Result;
use crate::proof::ProofResponse;
use crate::store::{Store, VersionInfo, WriteOp};

/// Cheap-to-clone shared handle to the service.
#[derive(Clone)]
pub struct Service {
    store: Arc<Store>,
    /// RocksDB publishes are atomic per batch, but two concurrent batches
    /// would still race when folding over the current state. Serialise them.
    write_lock: Arc<Mutex<()>>,
}

impl Service {
    /// Open the database at `path` (created if missing).
    pub fn open(path: impl AsRef<std::path::Path>) -> Result<Self> {
        let store = Arc::new(Store::open(path)?);
        Ok(Service { store, write_lock: Arc::new(Mutex::new(())) })
    }

    pub fn current_version(&self) -> Result<u64> {
        self.store.current_version()
    }

    /// Metadata for a version; version 0 is the implicit empty tree.
    pub fn version_info(&self, version: u64) -> Result<Option<VersionInfo>> {
        if version == 0 {
            return Ok(Some(VersionInfo {
                version: 0,
                root: crate::hash::empty_root(),
                leaf_count: 0,
                created_at_unix_ns: 0,
            }));
        }
        self.store.version_info(version)
    }

    pub fn list_versions(&self) -> Result<Vec<VersionInfo>> {
        self.store.list_versions()
    }

    /// Produce an existence or non-existence proof for `key` at `version`
    /// (defaults to the current version).
    pub fn prove(&self, key: &[u8], version: Option<u64>) -> Result<ProofResponse> {
        let v = version.unwrap_or_else(|| self.store.current_version().unwrap_or(0));
        let tree = self.store.tree_at(v)?;
        Ok(tree.prove(v, key))
    }

    /// Current live value of a key (existence and empty value distinguished).
    pub fn get(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        self.store.get_current(key)
    }

    /// Apply one write batch and publish the next immutable version.
    pub async fn apply_batch(&self, ops: Vec<WriteOp>) -> Result<VersionInfo> {
        let _guard = self.write_lock.lock().await;
        let store = self.store.clone();
        tokio::task::spawn_blocking(move || store.apply_batch(ops))
            .await
            .map_err(|e| crate::error::Error::Storage(format!("write task panicked: {e}")))?
    }
}
