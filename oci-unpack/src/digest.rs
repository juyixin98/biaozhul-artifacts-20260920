//! Real SHA-256 digest computation and OCI descriptor parsing.
//!
//! Descriptors use the canonical OCI form `sha256:<64 lowercase hex chars>`.
//! No digest is ever trusted from the manifest without recomputation.

use sha2::{Digest, Sha256};

use crate::error::{Error, Result};

/// A verified-or-unverified content digest, always algorithm-prefixed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DigestRef {
    pub algo: String,
    pub hex: String,
}

impl DigestRef {
    /// Parse `sha256:xxxx…`. Anything else (unknown algo, bad hex, bad length)
    /// is rejected up front.
    pub fn parse(s: &str) -> Result<Self> {
        let (algo, hexpart) = s
            .split_once(':')
            .ok_or_else(|| Error::UnsupportedDigest(s.to_string()))?;
        if algo != "sha256" {
            return Err(Error::UnsupportedDigest(s.to_string()));
        }
        if hexpart.len() != 64 || !hexpart.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Error::UnsupportedDigest(s.to_string()));
        }
        Ok(DigestRef {
            algo: algo.to_string(),
            hex: hexpart.to_ascii_lowercase(),
        })
    }

    pub fn as_str(&self) -> String {
        format!("{}:{}", self.algo, self.hex)
    }
}

/// Streaming hasher. Bytes are fed through [`Hasher::update`]; the final digest
/// is materialised with [`Hasher::finalize`] (the hasher is consumed).
#[derive(Default)]
pub struct Hasher {
    inner: Sha256,
    bytes: u64,
}

impl Hasher {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&mut self, chunk: &[u8]) {
        self.inner.update(chunk);
        self.bytes = self.bytes.saturating_add(chunk.len() as u64);
    }

    /// Total number of bytes hashed so far (the *compressed* on-disk size when
    /// wrapping a layer blob — used for decompression-bomb accounting too).
    pub fn bytes_hashed(&self) -> u64 {
        self.bytes
    }

    pub fn finalize(self) -> String {
        let out = self.inner.finalize();
        format!("sha256:{}", hex::encode(out))
    }
}

/// Convenience helper for hashing a whole byte slice (tests, small files).
pub fn sha256_hex(data: &[u8]) -> String {
    let mut h = Hasher::new();
    h.update(data);
    h.finalize()
}

/// Verify that `data` hashes to `expected`. Returns the actual digest on
/// failure so callers can report it honestly.
pub fn expect_digest(data: &[u8], expected: &DigestRef) -> Result<()> {
    let actual = sha256_hex(data);
    if actual == expected.as_str() {
        Ok(())
    } else {
        Err(Error::DigestMismatch {
            layer_index: usize::MAX,
            expected: expected.as_str(),
            actual,
        })
    }
}
