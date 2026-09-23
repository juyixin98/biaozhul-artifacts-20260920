//! Error types produced by the RESP2 parser.

use std::fmt;

/// All fatal, stream-level errors the hand-written parser can produce.
///
/// Once a [`crate::parser::Parser`] returns one of these it is "poisoned":
/// the protocol violation cannot be resynchronized from the byte stream alone,
/// so the connection should be closed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// The first byte of a frame is not one of `$`, `*`, `:`, `+`, `-`.
    UnknownTypeByte(u8),
    /// A line did not end with `\r\n`; `byte` is the offending CR (e.g. bare `\r\n`
    /// is valid, but `\rX` is reported with `b'X'`).
    InvalidLineEnding { byte: u8 },
    /// A length / integer field contained non-ASCII-digit characters or a sign
    /// in an illegal position. `field` names what was being parsed.
    InvalidInteger { field: &'static str, raw: Vec<u8> },
    /// An `:<n>` integer did not fit in `i64`.
    IntegerOverflow(Vec<u8>),
    /// A bulk `$<len>` length was negative but not the legal null marker `-1`.
    InvalidBulkLength(i64),
    /// Declared bulk length exceeds `Config::max_bulk_length`.
    BulkTooLarge { declared: usize, max: usize },
    /// Declared array length exceeds `Config::max_array_length`.
    ArrayTooLarge { declared: usize, max: usize },
    /// Nesting depth of arrays exceeds `Config::max_depth` (top-level frame = depth 0).
    NestingTooDeep { depth: usize, max: usize },
    /// A declared frame would push cumulative consumed bytes past `Config::max_total_bytes`.
    BudgetExceeded { needed: usize, remaining: usize },
    /// A `+`/`-`/`:`/`$`/`*` header line exceeded `Config::max_line_length`
    /// content bytes without a CRLF.
    LineTooLong { length: usize, max: usize },
    /// A simple string / error payload was not valid UTF-8 (binary-safe data
    /// must travel in bulk strings).
    InvalidUtf8 { field: &'static str, raw: Vec<u8> },
    /// `feed`/`try_next` was called after an earlier fatal error.
    ParserPoisoned,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::UnknownTypeByte(b) => {
                write!(f, "unknown RESP type byte: 0x{:02X} ({:?})", b, *b as char)
            }
            ParseError::InvalidLineEnding { byte } => {
                write!(f, "expected CRLF, got CR followed by 0x{:02X}", byte)
            }
            ParseError::InvalidInteger { field, raw } => write!(
                f,
                "invalid integer in field `{}`: {:?}",
                field,
                String::from_utf8_lossy(raw)
            ),
            ParseError::IntegerOverflow(raw) => write!(
                f,
                "integer {:?} does not fit in i64",
                String::from_utf8_lossy(raw)
            ),
            ParseError::InvalidBulkLength(n) => {
                write!(f, "illegal negative bulk string length: {} (only -1 null is allowed)", n)
            }
            ParseError::BulkTooLarge { declared, max } => write!(
                f,
                "bulk string length {} exceeds configured maximum {}",
                declared, max
            ),
            ParseError::ArrayTooLarge { declared, max } => write!(
                f,
                "array length {} exceeds configured maximum {}",
                declared, max
            ),
            ParseError::NestingTooDeep { depth, max } => write!(
                f,
                "array nesting depth {} exceeds configured maximum {}",
                depth, max
            ),
            ParseError::BudgetExceeded { needed, remaining } => write!(
                f,
                "total byte budget exceeded: frame needs {} bytes, {} remain",
                needed, remaining
            ),
            ParseError::LineTooLong { length, max } => write!(
                f,
                "header line of {} bytes exceeds configured maximum {}",
                length, max
            ),
            ParseError::InvalidUtf8 { field, raw } => write!(
                f,
                "non-UTF-8 payload in field `{}` ({} bytes)",
                field,
                raw.len()
            ),
            ParseError::ParserPoisoned => {
                f.write_str("parser is poisoned after a previous fatal error")
            }
        }
    }
}

impl std::error::Error for ParseError {}

/// Result of one parsing attempt when the stream has not produced a complete
/// frame (yet) or has fatally failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Poll<T> {
    /// A complete frame is ready; the consumed bytes have been removed from the
    /// internal buffer.
    Ready(T),
    /// More bytes are needed. This is *not* an error.
    Pending,
    /// Unrecoverable protocol violation.
    Error(ParseError),
}
