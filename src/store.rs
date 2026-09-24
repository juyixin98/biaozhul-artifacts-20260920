//! In-memory artifact store.
//!
//! Good enough for the exercise and the automated tests: artifacts live in a
//! `HashMap` behind a `Mutex` and are lost on restart.  Persistence is listed
//! in the README as not implemented.

use crate::error::{Error, Result};
use std::collections::HashMap;
use std::sync::Mutex;

#[derive(Default)]
pub struct ArtifactStore {
    artifacts: Mutex<HashMap<String, Vec<u8>>>,
}

/// Artifact names are path-safe so they can be used in URLs without escaping.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
}

impl ArtifactStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Create-or-replace an artifact; returns its new length.
    pub fn put(&self, name: &str, data: Vec<u8>) -> Result<u64> {
        if !valid_name(name) {
            return Err(Error::BadName);
        }
        let len = data.len() as u64;
        self.artifacts.lock().unwrap().insert(name.to_string(), data);
        Ok(len)
    }

    pub fn get(&self, name: &str) -> Result<Vec<u8>> {
        if !valid_name(name) {
            return Err(Error::BadName);
        }
        self.artifacts
            .lock()
            .unwrap()
            .get(name)
            .cloned()
            .ok_or_else(|| Error::NotFound(name.to_string()))
    }

    pub fn exists(&self, name: &str) -> bool {
        self.artifacts.lock().unwrap().contains_key(name)
    }

    /// `(length, BLAKE3 hex)` for an artifact.
    pub fn meta(&self, name: &str) -> Result<(u64, String)> {
        let data = self.get(name)?;
        Ok((data.len() as u64, hex(&crate::signature::strong_hash(&data))))
    }

    pub fn list(&self) -> Vec<serde_json::Value> {
        self.artifacts
            .lock()
            .unwrap()
            .iter()
            .map(|(name, data)| {
                serde_json::json!({
                    "name": name,
                    "length": data.len(),
                    "blake3": hex(&crate::signature::strong_hash(data)),
                })
            })
            .collect()
    }
}

pub fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{b:02x}");
    }
    s
}
