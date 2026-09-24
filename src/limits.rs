//! Extraction limits guarding against decompression bombs / resource
//! exhaustion. All counters are enforced while the archive streams — a
//! malicious layer can never be fully materialised before being rejected.

use std::fmt;

/// Carried inside an [`std::io::Error`] when a streaming limit trips, so the
/// archive layer can turn it into [`crate::error::AppError::Limit`] instead
/// of mistaking it for a corrupt archive.
#[derive(Debug, Clone)]
pub struct LimitExceeded(pub String);

impl fmt::Display for LimitExceeded {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for LimitExceeded {}

/// Hard limits applied to a single image rebuild.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Maximum uncompressed size of one layer (bytes).
    pub max_layer_uncompressed: u64,
    /// Maximum number of entries per layer (including skipped pax headers).
    pub max_entries_per_layer: u64,
    /// Maximum total uncompressed bytes across all layers of an image.
    pub max_total_uncompressed: u64,
    /// Maximum length of a single entry path or link target, in bytes.
    pub max_path_len: usize,
    /// Maximum size of a single regular file, in bytes.
    pub max_file_size: u64,
    /// Maximum count of regular files written (including hardlinks).
    pub max_file_count: u64,
    /// Maximum size of a blob digest-verification read pass, in bytes.
    /// Must be at least as large as `max_layer_uncompressed` for gzip layers,
    /// since decompression happens during verification.
    pub max_verify_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_layer_uncompressed: 256 * 1024 * 1024, // 256 MiB
            max_entries_per_layer: 20_000,
            max_total_uncompressed: 1024 * 1024 * 1024, // 1 GiB
            max_path_len: 4096,
            max_file_size: 128 * 1024 * 1024, // 128 MiB
            max_file_count: 50_000,
            max_verify_bytes: 256 * 1024 * 1024,
        }
    }
}
