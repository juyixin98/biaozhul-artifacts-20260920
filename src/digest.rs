//! OCI digests.
//!
//! Only `sha256:<64 lowercase hex chars>` is supported, which is the
//! mandatory algorithm for OCI image layouts. All cryptographic operations
//! are real SHA-256 computations performed with the `sha2` crate.

use std::fmt;

use sha2::{Digest as _, Sha256};

/// A validated OCI content digest (sha256 only).
#[derive(Clone, PartialEq, Eq, Hash)]
pub struct OciDigest {
    hex: String,
}

impl OciDigest {
    /// Compute the sha256 digest of `data`.
    pub fn of_bytes(data: &[u8]) -> Self {
        let mut h = Sha256::new();
        h.update(data);
        Self {
            hex: hex::encode(h.finalize()),
        }
    }

    /// Parse and validate a digest string like `sha256:abcd…`.
    pub fn parse(s: &str) -> Result<Self, DigestError> {
        let rest = s
            .strip_prefix("sha256:")
            .ok_or(DigestError::UnsupportedAlgorithm)?;
        if rest.len() != 64 || !rest.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(DigestError::Malformed);
        }
        // Normalise to lowercase so equality is canonical.
        Ok(Self {
            hex: rest.to_ascii_lowercase(),
        })
    }

    /// Hex part only (without the `sha256:` prefix).
    pub fn hex(&self) -> &str {
        &self.hex
    }

    /// Constant-time comparison against a freshly computed digest.
    pub fn verify(&self, actual: &OciDigest) -> bool {
        // `Sha256` output is 32 bytes; compare raw bytes with `ct_eq`-style
        // manual accumulation instead of `==` so verification does not leak
        // the first differing byte through timing.
        let a = hex::decode(&self.hex).expect("validated at construction");
        let b = hex::decode(&actual.hex).expect("validated at construction");
        let mut diff = 0u8;
        for (x, y) in a.iter().zip(b.iter()) {
            diff |= x ^ y;
        }
        diff == 0
    }
}

impl fmt::Display for OciDigest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "sha256:{}", self.hex)
    }
}

impl fmt::Debug for OciDigest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "OciDigest({self})")
    }
}

impl serde::Serialize for OciDigest {
    fn serialize<S: serde::Serializer>(&self, ser: S) -> Result<S::Ok, S::Error> {
        ser.serialize_str(&self.to_string())
    }
}

impl<'de> serde::Deserialize<'de> for OciDigest {
    fn deserialize<D: serde::Deserializer<'de>>(de: D) -> Result<Self, D::Error> {
        let s = String::deserialize(de)?;
        OciDigest::parse(&s).map_err(serde::de::Error::custom)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum DigestError {
    #[error("unsupported digest algorithm (only sha256 is accepted)")]
    UnsupportedAlgorithm,
    #[error("malformed digest")]
    Malformed,
}

/// Streaming SHA-256 hasher with a running byte counter, used to verify
/// layer/config blobs while reading (and decompressing) them exactly once.
pub struct StreamHasher {
    hasher: Sha256,
    bytes: u64,
}

impl StreamHasher {
    pub fn new() -> Self {
        Self {
            hasher: Sha256::new(),
            bytes: 0,
        }
    }

    pub fn update(&mut self, chunk: &[u8]) {
        self.hasher.update(chunk);
        self.bytes = self.bytes.saturating_add(chunk.len() as u64);
    }

    pub fn bytes_read(&self) -> u64 {
        self.bytes
    }

    pub fn finalize(self) -> OciDigest {
        OciDigest {
            hex: hex::encode(self.hasher.finalize()),
        }
    }
}

impl Default for StreamHasher {
    fn default() -> Self {
        Self::new()
    }
}

impl std::io::Write for StreamHasher {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.update(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
