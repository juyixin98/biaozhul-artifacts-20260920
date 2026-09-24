//! Content digest handling (OCI style: `algorithm:encoded_hex`).
//!
//! Only SHA-256 digests are produced by this service, but structurally valid
//! digests using other algorithms are parsed so that clear error messages can
//! be returned when they cannot be verified.

use sha2::{Digest as _, Sha256};
use std::fmt;

/// A validated digest reference such as `sha256:abcdef...`.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Digest(String);

/// Error returned when a digest string is not well formed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DigestParseError(pub String);

impl fmt::Display for DigestParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid digest: {}", self.0)
    }
}

impl std::error::Error for DigestParseError {}

impl Digest {
    /// Parse and validate an `algorithm:hex` digest string.
    pub fn parse(raw: &str) -> Result<Digest, DigestParseError> {
        let Some((algo, encoded)) = raw.split_once(':') else {
            return Err(DigestParseError(format!(
                "{raw:?}: expected \"algorithm:hex\""
            )));
        };
        if algo.is_empty()
            || !algo
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        {
            return Err(DigestParseError(format!(
                "{raw:?}: algorithm segment must be lowercase ascii/digits"
            )));
        }
        if encoded.is_empty() || !encoded.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(DigestParseError(format!(
                "{raw:?}: encoded segment must be hex"
            )));
        }
        if algo == "sha256" && encoded.len() != 64 {
            return Err(DigestParseError(format!(
                "{raw:?}: sha256 digest must be 64 hex characters"
            )));
        }
        Ok(Digest(raw.to_string()))
    }

    /// Compute the canonical SHA-256 digest of `bytes`.
    pub fn sha256(bytes: &[u8]) -> Digest {
        let mut hasher = Sha256::new();
        hasher.update(bytes);
        Digest(format!("sha256:{}", hex::encode(hasher.finalize())))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn algorithm(&self) -> &str {
        self.0.split_once(':').unwrap().0
    }
}

impl fmt::Display for Digest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl TryFrom<String> for Digest {
    type Error = DigestParseError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        Digest::parse(&value)
    }
}

impl From<Digest> for String {
    fn from(value: Digest) -> String {
        value.0
    }
}

/// Verify that `bytes` hashes to `claimed`. Unsupported algorithms fail.
pub fn verify(claimed: &Digest, bytes: &[u8]) -> Result<(), Digest> {
    if claimed.algorithm() != "sha256" {
        // We cannot verify anything else; report the value we *can* compute.
        return Err(Digest::sha256(bytes));
    }
    let actual = Digest::sha256(bytes);
    if &actual == claimed {
        Ok(())
    } else {
        Err(actual)
    }
}
