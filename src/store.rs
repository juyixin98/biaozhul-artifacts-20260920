//! In-memory, strictly local content store.
//!
//! Content is organized per repository, mirroring a registry layout:
//! - repositories have named tags pointing at manifest digests;
//! - blobs are addressed by digest and hold raw bytes.
//!
//! Nothing here performs network I/O: a descriptor that points at a blob the
//! store does not contain yields [`ApiError::MissingBlob`].

use std::collections::BTreeMap;

use crate::digest::Digest;
use crate::error::{ApiError, ApiResult};
use crate::model::BlobKind;

/// One stored blob: its exact bytes and parsed kind.
pub struct Blob {
    pub bytes: Vec<u8>,
    pub kind: BlobKind,
}

#[derive(Default)]
pub struct Repo {
    /// tag name -> manifest digest
    pub tags: BTreeMap<String, String>,
    /// digest -> blob
    pub blobs: BTreeMap<String, Blob>,
}

#[derive(Default)]
pub struct Store {
    pub repos: BTreeMap<String, Repo>,
}

impl Store {
    pub fn new() -> Self {
        Store::default()
    }

    pub fn repo(&self, name: &str) -> Result<&Repo, ApiError> {
        self.repos
            .get(name)
            .ok_or_else(|| ApiError::NotFound(format!("repository {name:?} not found")))
    }

    pub fn repo_mut(&mut self, name: &str) -> &mut Repo {
        self.repos.entry(name.to_string()).or_default()
    }

    pub fn put_tag(&mut self, repo: &str, tag: String, digest: String) -> Result<(), ApiError> {
        let repo_obj = self.repo_mut(repo);
        if !repo_obj.blobs.contains_key(&digest) {
            return Err(ApiError::NotFound(format!(
                "cannot tag {repo}:{tag}: blob {digest} not found in repository"
            )));
        }
        repo_obj.tags.insert(tag, digest);
        Ok(())
    }

    pub fn resolve_tag(&self, repo: &str, tag: &str) -> Result<&str, ApiError> {
        let repo_obj = self.repo(repo)?;
        repo_obj.tags.get(tag).map(String::as_str).ok_or_else(|| {
            ApiError::NotFound(format!("tag {tag:?} not found in repository {repo:?}"))
        })
    }

    /// Fetch a blob for a digest, verifying integrity on every access.
    pub fn get(&self, repo: &str, digest: &Digest) -> ApiResult<&Blob> {
        let repo_obj = self.repo(repo)?;
        let blob = repo_obj
            .blobs
            .get(digest.as_str())
            .ok_or_else(|| ApiError::MissingBlob {
                digest: digest.to_string(),
                at: format!("repository {repo:?}"),
            })?;
        crate::digest::verify(digest, &blob.bytes).map_err(|actual| ApiError::DigestMismatch {
            claimed: digest.to_string(),
            actual: actual.to_string(),
            at: format!("repository {repo:?}"),
        })?;
        Ok(blob)
    }
}
