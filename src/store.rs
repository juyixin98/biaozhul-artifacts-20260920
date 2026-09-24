//! In-memory, content-addressed registry storage.
//!
//! Two maps live behind one lock:
//! * `blobs`  — digest → raw bytes, with a `trusted` flag. Blobs uploaded
//!   through the HTTP API are always digest-verified first. Blobs mounted
//!   from a local fixture directory may be marked trusted (used for the
//!   mandatory cycle demo: a mathematically content-addressed graph cannot
//!   contain a cycle, so the fixture simulates a manifest list that refers
//!   back to itself).
//! * `tags`   — (repository, tag) → manifest digest.

use std::collections::HashMap;
use std::sync::RwLock;

use serde::Serialize;

use crate::digest::{Digest, DigestError};

#[derive(Debug, Clone)]
struct StoredBlob {
    data: Vec<u8>,
    /// False when bytes are guaranteed to hash to their key (uploads).
    /// True for fixture mounts whose content may intentionally not match
    /// their declared descriptor (corruption / cycle scenarios).
    trusted: bool,
}

/// Thread-safe registry state.
#[derive(Debug, Default)]
pub struct Registry {
    blobs: RwLock<HashMap<String, StoredBlob>>,
    tags: RwLock<HashMap<(String, String), String>>,
}

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error(transparent)]
    Digest(#[from] DigestError),
    #[error("blob already exists with different content: {0}")]
    AlreadyExists(String),
    #[error("blob not found: {0}")]
    NotFound(String),
}

/// Result of a verification pass exposed to callers (fixtures/API).
#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    pub digest: String,
    pub size: usize,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Registry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Store an uploaded blob after verifying it hashes to `digest`.
    pub fn put_verified(&self, digest: &str, data: Vec<u8>) -> Result<Digest, StoreError> {
        let d = crate::digest::verify(digest, &data)?;
        let mut blobs = self.blobs.write().unwrap();
        if let Some(existing) = blobs.get(d.as_str()) {
            if existing.data != data {
                return Err(StoreError::AlreadyExists(d.to_string()));
            }
        } else {
            blobs.insert(
                d.to_string(),
                StoredBlob {
                    data,
                    trusted: false,
                },
            );
        }
        Ok(d)
    }

    /// Mount bytes under `digest` without verifying at write time.
    ///
    /// `trusted == true` means the key itself is synthetic (the cycle demo
    /// uses `sha256:aaa…`); the selector skips content verification for it
    /// even in strict mode (there is no "correct" content to compare).
    ///
    /// `trusted == false` models storage tampering: the key is a real
    /// content digest but the bytes no longer match; strict selection must
    /// detect it.
    pub fn put_trusted(&self, digest: String, data: Vec<u8>, trusted: bool) {
        let mut blobs = self.blobs.write().unwrap();
        blobs.insert(
            digest,
            StoredBlob { data, trusted },
        );
    }

    /// Content-addressed insert: digest is computed from bytes.
    pub fn put_content(&self, data: Vec<u8>) -> Digest {
        let d = Digest::of_bytes(&data);
        let mut blobs = self.blobs.write().unwrap();
        blobs.entry(d.to_string()).or_insert(StoredBlob {
            data,
            trusted: false,
        });
        d
    }

    pub fn get_blob(&self, digest: &str) -> Option<Vec<u8>> {
        self.blobs
            .read()
            .unwrap()
            .get(digest)
            .map(|b| b.data.clone())
    }

    pub fn has_blob(&self, digest: &str) -> bool {
        self.blobs.read().unwrap().contains_key(digest)
    }

    pub fn is_trusted(&self, digest: &str) -> bool {
        self.blobs
            .read()
            .unwrap()
            .get(digest)
            .map(|b| b.trusted)
            .unwrap_or(false)
    }

    pub fn tag(&self, repo: &str, tag: &str, digest: String) {
        let _ = Digest::parse(&digest).expect("tags only hold validated digests");
        self.tags
            .write()
            .unwrap()
            .insert((repo.to_string(), tag.to_string()), digest);
    }

    pub fn resolve_tag(&self, repo: &str, tag: &str) -> Option<String> {
        self.tags
            .read()
            .unwrap()
            .get(&(repo.to_string(), tag.to_string()))
            .cloned()
    }

    pub fn list_tags(&self, repo: &str) -> Vec<String> {
        let mut tags: Vec<String> = self
            .tags
            .read()
            .unwrap()
            .keys()
            .filter(|(r, _)| r == repo)
            .map(|(_, t)| t.clone())
            .collect();
        tags.sort();
        tags
    }

    pub fn repositories(&self) -> Vec<String> {
        let mut repos: Vec<String> = self
            .tags
            .read()
            .unwrap()
            .keys()
            .map(|(r, _)| r.clone())
            .collect();
        repos.sort();
        repos.dedup();
        repos
    }

    /// Recompute digests for every blob. Used by the admin verify endpoint
    /// and by tests to observe the corrupted fixture.
    pub fn verify_all(&self) -> Vec<VerifyReport> {
        let blobs = self.blobs.read().unwrap();
        let mut reports: Vec<(&String, &StoredBlob, bool, Option<String>)> = blobs
            .iter()
            .map(|(key, blob)| {
                match crate::digest::verify(key, &blob.data) {
                    Ok(_) => (key, blob, true, None),
                    Err(e) => (key, blob, false, Some(e.to_string())),
                }
            })
            .collect();
        reports.sort_by(|a, b| a.0.cmp(b.0));
        reports
            .into_iter()
            .map(|(digest, blob, ok, error)| VerifyReport {
                digest: digest.clone(),
                size: blob.data.len(),
                ok,
                error,
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_tampered_upload() {
        let reg = Registry::new();
        let good = Digest::of_bytes(b"abc").to_string();
        assert!(reg.put_verified(&good, b"abc".to_vec()).is_ok());
        let bad = Digest::of_bytes(b"other").to_string();
        assert!(reg.put_verified(&bad, b"abc".to_vec()).is_err());
    }

    #[test]
    fn tags_round_trip() {
        let reg = Registry::new();
        let d = reg.put_content(b"manifest-bytes".to_vec());
        reg.tag("demo/app", "latest", d.to_string());
        assert_eq!(reg.resolve_tag("demo/app", "latest"), Some(d.to_string()));
        assert_eq!(reg.list_tags("demo/app"), vec!["latest"]);
    }

    #[test]
    fn verify_all_reports_corruption() {
        let reg = Registry::new();
        reg.put_verified(
            &Digest::of_bytes(b"x").to_string(),
            b"x".to_vec(),
        )
        .unwrap();
        reg.put_trusted(Digest::of_bytes(b"y").to_string(), b"tampered".to_vec(), false);
        let reports = reg.verify_all();
        assert_eq!(reports.len(), 2);
        let bad = reports.iter().filter(|r| !r.ok).count();
        assert_eq!(bad, 1);
    }
}
