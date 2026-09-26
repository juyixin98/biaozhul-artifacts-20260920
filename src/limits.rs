//! Resource limits bounding memory consumption and decoder output size.
//!
//! All limits are explicit; [`Limits::default`] is deliberately conservative
//! (64 MiB message, depth 64). The decoder enforces `max_message_bytes`
//! against *every byte read* (including skipped unknown fields), so hostile
//! input cannot exhaust memory.

/// Configuration for bounded decoding/encoding.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Maximum total bytes the decoder will read from a stream (header
    /// excluded), counting unknown-field payloads.
    pub max_message_bytes: u64,
    /// Maximum bytes the encoder will produce.
    pub max_output_bytes: u64,
    /// Maximum nested-message depth.
    pub max_nesting: u32,
    /// Maximum number of values accepted for one repeated field.
    pub max_repeated: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_message_bytes: 64 * 1024 * 1024,
            max_output_bytes: 64 * 1024 * 1024,
            max_nesting: 64,
            max_repeated: 1_000_000,
        }
    }
}

impl Limits {
    pub fn new() -> Limits {
        Limits::default()
    }

    pub fn with_max_message_bytes(mut self, n: u64) -> Self {
        self.max_message_bytes = n;
        self
    }

    pub fn with_max_output_bytes(mut self, n: u64) -> Self {
        self.max_output_bytes = n;
        self
    }

    pub fn with_max_nesting(mut self, n: u32) -> Self {
        self.max_nesting = n;
        self
    }

    pub fn with_max_repeated(mut self, n: usize) -> Self {
        self.max_repeated = n;
        self
    }
}
