//! Error types shared by the codec and the set implementation.

use std::fmt;

/// All fallible operations return [`RbError`]. The crate never panics on
/// untrusted input — malformed bytes, declared lengths that overrun the
/// stream, and limits that would be exceeded all become errors here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RbError {
    /// Input ended before a value or a declared payload could be read.
    UnexpectedEof {
        /// What the decoder was trying to read.
        what: &'static str,
        /// Bytes still needed.
        needed: u64,
        /// Bytes actually available.
        available: u64,
    },
    /// A declared length (key count, bitmap run payload, …) is larger than
    /// the configured limit or would overflow addressable memory.
    LengthExceeded {
        /// Which length was rejected.
        what: &'static str,
        /// Declared length in the stream.
        declared: u64,
        /// Effective limit.
        limit: u64,
    },
    /// A tag byte did not match any known container or run opcode.
    InvalidTag {
        /// Position-ish context string.
        ctx: &'static str,
        /// Raw tag value.
        tag: u8,
    },
    /// A high key was encountered twice while decoding (keys must be
    /// strictly ascending so streaming union stays correct).
    DuplicateKey(u16),
    /// Keys appeared out of ascending order.
    OutOfOrderKeys {
        /// Previous key.
        prev: u16,
        /// Current key.
        current: u16,
    },
    /// A value that must fit in 16 bits (container-local value, run
    /// endpoint, …) did not.
    ValueOutOfRange(u32),
    /// Two encoded operands had incompatible binary formats.
    FormatMismatch(&'static str),
    /// A UTF-8 or JSON-level failure at the control entry.
    InvalidInput(String),
    /// Everything else.
    Other(String),
}

impl fmt::Display for RbError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RbError::UnexpectedEof { what, needed, available } => write!(
                f,
                "unexpected end of input while reading {what}: need {needed} bytes, {available} available"
            ),
            RbError::LengthExceeded { what, declared, limit } => write!(
                f,
                "declared {what} length {declared} exceeds limit {limit}"
            ),
            RbError::InvalidTag { ctx, tag } => {
                write!(f, "invalid tag 0x{tag:02x} while decoding {ctx}")
            }
            RbError::DuplicateKey(k) => write!(f, "duplicate high key {k} in encoded input"),
            RbError::OutOfOrderKeys { prev, current } => write!(
                f,
                "keys out of order: {current} after {prev} (must be strictly ascending)"
            ),
            RbError::ValueOutOfRange(v) => {
                write!(f, "container value {v} does not fit in 16 bits")
            }
            RbError::FormatMismatch(s) => write!(f, "format mismatch: {s}"),
            RbError::InvalidInput(s) => write!(f, "invalid input: {s}"),
            RbError::Other(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for RbError {}

/// Crate-wide result alias.
pub type RbResult<T> = Result<T, RbError>;
