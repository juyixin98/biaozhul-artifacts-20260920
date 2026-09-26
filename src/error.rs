//! Error types for the lzsw codec.

use std::fmt;

/// All failure modes of encoding/decoding. The `kind` string is stable and
/// used verbatim in the JSON control interface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Stream does not start with the "LZSW" magic bytes.
    InvalidMagic,
    /// Header version byte is not supported.
    UnsupportedVersion(u8),
    /// Header flags/reserved bytes are non-zero.
    InvalidHeader,
    /// Header requests a window other than 2^15.
    UnsupportedWindow(u8),
    /// A match token refers to a distance larger than the window.
    DistanceExceedsWindow { distance: u32 },
    /// A match token refers to bytes before the start of the output.
    DistanceTooLarge { distance: u32, produced: u64 },
    /// Input ended in the middle of a token (or header).
    TruncatedToken,
    /// Decoding would exceed the configured output budget.
    OutputBudgetExceeded { limit: u64, attempted: u64 },
    /// `update` called after `finish`.
    AlreadyFinished,
}

impl Error {
    /// Stable machine-readable kind, mirrored by the JSON interface.
    pub fn kind(&self) -> &'static str {
        match self {
            Error::InvalidMagic => "InvalidMagic",
            Error::UnsupportedVersion(_) => "UnsupportedVersion",
            Error::InvalidHeader => "InvalidHeader",
            Error::UnsupportedWindow(_) => "UnsupportedWindow",
            Error::DistanceExceedsWindow { .. } => "DistanceExceedsWindow",
            Error::DistanceTooLarge { .. } => "DistanceTooLarge",
            Error::TruncatedToken => "TruncatedToken",
            Error::OutputBudgetExceeded { .. } => "OutputBudgetExceeded",
            Error::AlreadyFinished => "AlreadyFinished",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidMagic => write!(f, "invalid magic bytes"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported version {v}"),
            Error::InvalidHeader => write!(f, "invalid header flags"),
            Error::UnsupportedWindow(w) => write!(f, "unsupported window log2 {w}"),
            Error::DistanceExceedsWindow { distance } => {
                write!(f, "match distance {distance} exceeds window size")
            }
            Error::DistanceTooLarge { distance, produced } => write!(
                f,
                "match distance {distance} exceeds bytes produced so far ({produced})"
            ),
            Error::TruncatedToken => write!(f, "input ends mid-token"),
            Error::OutputBudgetExceeded { limit, attempted } => {
                write!(f, "output budget {limit} exceeded (attempted {attempted})")
            }
            Error::AlreadyFinished => write!(f, "stream already finished"),
        }
    }
}

impl std::error::Error for Error {}
