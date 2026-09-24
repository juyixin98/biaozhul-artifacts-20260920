//! In-memory artifact store.
//!
//! Artifacts are held in RAM keyed by an ID. Total retained bytes are capped
//! (`ARTIFACT_STORE_MAX_BYTES`, default 1 GiB); a PUT that would exceed the
//! cap is rejected with 413. This keeps the demo service dependency-free —
//! there is deliberately no disk persistence (see README "Limitations").

use std::collections::HashMap;
use std::sync::RwLock;

use crate::Error;

#[derive(Debug)]
pub struct Store {
    artifacts: RwLock<HashMap<String, Vec<u8>>>,
    total_bytes: RwLock<usize>,
    max_bytes: usize,
}

impl Store {
    pub fn new(max_bytes: usize) -> Self {
        Store {
            artifacts: RwLock::new(HashMap::new()),
            total_bytes: RwLock::new(0),
            max_bytes,
        }
    }

    /// Store (or replace) an artifact.
    pub fn put(&self, id: &str, data: Vec<u8>) -> Result<(), Error> {
        let mut map = self.artifacts.write().unwrap();
        let mut total = self.total_bytes.write().unwrap();
        let old = map.get(id).map(|v| v.len()).unwrap_or(0);
        let new_total = *total - old + data.len();
        if new_total > self.max_bytes {
            return Err(Error::payload_too_large(format!(
                "store capacity {} bytes exceeded (would hold {new_total} bytes)",
                self.max_bytes
            )));
        }
        *total = new_total;
        map.insert(id.to_string(), data);
        Ok(())
    }

    pub fn get(&self, id: &str) -> Option<Vec<u8>> {
        self.artifacts.read().unwrap().get(id).cloned()
    }

    pub fn contains(&self, id: &str) -> bool {
        self.artifacts.read().unwrap().contains_key(id)
    }
}
