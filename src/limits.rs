//! Resource limits applied while decoding untrusted streams.
//!
//! Every limit is checked *before* the corresponding allocation or output
//! buffer is created, so a hostile header cannot trigger huge allocations.

/// Hard ceiling for the encoder's block size, independent of user limits.
pub const ABSOLUTE_MAX_BLOCK_SIZE: u32 = 1 << 20;

/// Resource limits for decoding. All fields are checked before allocation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// Maximum accepted input size in bytes.
    pub max_input_bytes: usize,
    /// Maximum total number of values a stream may declare.
    pub max_total_values: u64,
    /// Maximum values per block (also caps per-block allocation).
    pub max_block_size: u32,
    /// Maximum number of values a decode operation may return.
    pub max_output_values: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_input_bytes: 64 * 1024 * 1024,
            max_total_values: 1 << 22,
            max_block_size: 1 << 16,
            max_output_values: 1 << 22,
        }
    }
}

impl Limits {
    /// Limits that accept anything structurally valid. Useful in tests that
    /// exercise the format itself rather than the guard rails.
    #[cfg(test)]
    pub(crate) fn unrestricted() -> Self {
        Limits {
            max_input_bytes: usize::MAX,
            max_total_values: u64::MAX,
            max_block_size: ABSOLUTE_MAX_BLOCK_SIZE,
            max_output_values: u64::MAX,
        }
    }
}
