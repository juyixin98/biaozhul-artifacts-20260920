//! SHA-256 helpers.
//!
//! The hash is used **only** for:
//! - content addressing (the on-disk block path is derived from the hash);
//! - deduplication (identical content maps to the same hash);
//! - integrity verification (bytes on disk are re-hashed and compared).
//!
//! It is deliberately *not* treated as a security boundary: the store does not
//! assume hash collisions are unfeasible in the face of an adversary, and
//! uploading blocks is an unauthenticated operation.

use sha2::{Digest, Sha256};

/// Number of hex characters in a SHA-256 hash string.
pub const HASH_HEX_LEN: usize = 64;

/// Compute the lowercase hex SHA-256 of `data`.
pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut s = String::with_capacity(HASH_HEX_LEN);
    for b in digest {
        use std::fmt::Write;
        let _ = write!(&mut s, "{:02x}", b);
    }
    s
}

/// Validate a **lowercase** 64-character hex SHA-256 string.
///
/// Uppercase is rejected on purpose so that block addresses have a single
/// canonical spelling on disk.
pub fn is_valid_hash(s: &str) -> bool {
    s.len() == HASH_HEX_LEN
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Validate a root name.
///
/// Root names are used verbatim as filenames under `roots/`, so they must not
/// contain path separators, NUL, `.`/`..` or leading dots (which are reserved
/// for temporary files).
pub fn is_valid_root_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && !name.starts_with('.')
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains('\0')
        && name != "."
        && name != ".."
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vector() {
        // SHA-256("abc")
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn hash_validation() {
        assert!(is_valid_hash(&"a".repeat(64)));
        assert!(!is_valid_hash(&"A".repeat(64))); // uppercase rejected
        assert!(!is_valid_hash(&"g".repeat(64)));
        assert!(!is_valid_hash(&"a".repeat(63)));
        assert!(!is_valid_hash(&"a".repeat(65)));
    }

    #[test]
    fn root_name_validation() {
        assert!(is_valid_root_name("main"));
        assert!(is_valid_root_name("release-2026_09"));
        assert!(!is_valid_root_name(""));
        assert!(!is_valid_root_name("."));
        assert!(!is_valid_root_name(".."));
        assert!(!is_valid_root_name(".hidden"));
        assert!(!is_valid_root_name("a/b"));
        assert!(!is_valid_root_name("a\\b"));
    }
}
