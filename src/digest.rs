//! OCI digest helpers.
//!
//! Only the algorithm required by the acceptance criteria is implemented:
//! `sha256:<64 lowercase hex chars>`, as defined by the OCI image spec.

use sha2::{Digest as _, Sha256};

/// A parsed, validated OCI digest (`sha256:<hex>`).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Digest(String);

/// Errors that can occur while parsing or verifying a digest string.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DigestError {
    #[error("invalid digest `{0}`: expected `<algorithm>:<hex>`, e.g. sha256:...")]
    BadFormat(String),
    #[error("unsupported digest algorithm `{0}` (only sha256 is supported)")]
    UnsupportedAlgorithm(String),
    #[error("invalid sha256 hex body `{0}`: expected 64 lowercase hex characters")]
    BadHex(String),
    #[error("digest mismatch: descriptor declares {declared}, content is {computed}")]
    Mismatch { declared: String, computed: String },
}

impl Digest {
    /// Parse and validate a digest string such as `sha256:a3ed...`.
    pub fn parse(s: &str) -> Result<Digest, DigestError> {
        let (algo, body) = s
            .split_once(':')
            .ok_or_else(|| DigestError::BadFormat(s.to_string()))?;
        if algo.is_empty() || body.is_empty() {
            return Err(DigestError::BadFormat(s.to_string()));
        }
        if algo != "sha256" {
            return Err(DigestError::UnsupportedAlgorithm(algo.to_string()));
        }
        if body.len() != 64
            || !body
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Err(DigestError::BadHex(body.to_string()));
        }
        Ok(Digest(s.to_string()))
    }

    /// Compute the canonical digest of raw content.
    pub fn of_bytes(data: &[u8]) -> Digest {
        let mut h = Sha256::new();
        h.update(data);
        Digest(format!("sha256:{}", hex::encode(h.finalize())))
    }

    /// The canonical `algorithm:hex` string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for Digest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl serde::Serialize for Digest {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.0)
    }
}

/// Compare the digest declared on a descriptor with the digest recomputed
/// from the bytes it points at. Returns the canonical digest on success,
/// or [`DigestError::Mismatch`] when they differ.
pub fn verify(expected: &str, actual_bytes: &[u8]) -> Result<Digest, DigestError> {
    let declared = Digest::parse(expected)?;
    let computed = Digest::of_bytes(actual_bytes);
    if declared != computed {
        return Err(DigestError::Mismatch {
            declared: declared.to_string(),
            computed: computed.to_string(),
        });
    }
    Ok(computed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_empty_sha256() {
        let d = Digest::of_bytes(b"");
        assert_eq!(
            d.to_string(),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn roundtrip_parse() {
        let d = Digest::of_bytes(b"hello");
        let parsed = Digest::parse(&d.to_string()).unwrap();
        assert_eq!(d, parsed);
    }

    #[test]
    fn rejects_bad_inputs() {
        assert!(Digest::parse("notadigest").is_err());
        assert!(Digest::parse("sha512:abcd").is_err());
        assert!(Digest::parse("sha256:ABC").is_err());
        assert!(Digest::parse("sha256:zz").is_err());
        // uppercase hex rejected (OCI requires lowercase)
        assert!(Digest::parse(
            "sha256:E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"
        )
        .is_err());
    }

    #[test]
    fn verify_detects_tampering() {
        let good = Digest::of_bytes(b"payload").to_string();
        assert!(verify(&good, b"payload").is_ok());
        let err = verify(&good, b"tampered").unwrap_err();
        assert!(matches!(err, DigestError::Mismatch { .. }));
        assert!(err.to_string().contains("digest mismatch"));
    }
}
