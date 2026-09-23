//! Error and warning types for the observer.
//!
//! The supported subset and the length caps are part of the public API of the
//! error model: every way a byte stream can fail to match the accepted subset
//! maps to an explicit [`ParseError`] variant instead of a generic failure.

use std::fmt;

/// Which syntactic layer was being parsed when an error occurred.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layer {
    /// The 5-byte TLS record header (`content_type`, version, length).
    RecordHeader,
    /// The TLS record payload / fragment reassembly stage.
    Record,
    /// The 4-byte handshake header (`handshake_type`, length).
    HandshakeHeader,
    /// The handshake message body (`ClientHello`).
    HandshakeBody,
    /// An extension block inside `ClientHello`.
    Extension,
}

impl Layer {
    pub fn as_str(self) -> &'static str {
        match self {
            Layer::RecordHeader => "record_header",
            Layer::Record => "record",
            Layer::HandshakeHeader => "handshake_header",
            Layer::HandshakeBody => "handshake_body",
            Layer::Extension => "extension",
        }
    }
}

/// At which point a needed length field overran the available bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Truncated {
    /// A fixed-width field could not be read completely.
    Field,
    /// A length-prefixed vector declared more bytes than remained.
    LengthPrefixed,
    /// The input ended while record fragments for one handshake message were
    /// still being reassembled.
    Reassembly,
}

impl Truncated {
    pub fn as_str(self) -> &'static str {
        match self {
            Truncated::Field => "field",
            Truncated::LengthPrefixed => "length_prefixed",
            Truncated::Reassembly => "reassembly",
        }
    }
}

/// Which length prefix disagreed with the bytes that actually followed it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LengthMismatchKind {
    /// An inner length disagreed with the enclosing declared length.
    Nested,
    /// Bytes marked consumed did not equal the enclosing declared length.
    Consumed,
    /// A vector carried a length that violates the TLS field rules
    /// (odd cipher-suite list, zero cipher suites, ...).
    Field,
}

impl LengthMismatchKind {
    pub fn as_str(self) -> &'static str {
        match self {
            LengthMismatchKind::Nested => "nested",
            LengthMismatchKind::Consumed => "consumed",
            LengthMismatchKind::Field => "field",
        }
    }
}

/// All parse failures accepted by the observer's supported subset.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// Not enough bytes to finish the element at `layer` (`where_` says which
    /// read failed). This is *incomplete input*; feeding more bytes may clear
    /// it unless `finish()` proves the stream itself ended.
    Truncated { layer: Layer, where_: Truncated },
    /// Record layer: `content_type` byte is not in the accepted subset
    /// (20 ChangeCipherSpec / 21 Alert / 22 Handshake / 23 ApplicationData).
    BadContentType(u8),
    /// Record layer: fixed part `legacy_version` is not `{0x03, 0x01..=0x03}`.
    InvalidRecordVersion { major: u8, minor: u8 },
    /// Record layer: fragment length exceeds the accepted cap
    /// (`max_record_fragment`, RFC 8446 upper bound 2^14 + 256).
    RecordTooLarge { length: usize, max: usize },
    /// Handshake layer: declared message length exceeds the configured
    /// reassembly cap (`max_handshake_message`).
    HandshakeTooLarge { length: usize, max: usize },
    /// Handshake layer: the 4-byte header names a type outside the accepted
    /// subset (only 1 ClientHello is a handshake; others are reported as
    /// skipped, not errors, unless they appear where framing is impossible).
    UnsupportedHandshakeType(u8),
    /// `ClientHello.client_version` is not an accepted legacy version
    /// (0x0301 TLS 1.0 .. 0x0304 legacy for TLS 1.3).
    InvalidClientHelloVersion { major: u8, minor: u8 },
    /// A length prefix disagrees with its enclosing container.
    /// `what` names the offending field (e.g. "client_hello.extensions").
    LengthMismatch {
        layer: Layer,
        kind: LengthMismatchKind,
        what: &'static str,
        declared: usize,
        actual: usize,
    },
    /// A variable-length vector violates the field rules
    /// (e.g. odd cipher-suites list length).
    MalformedVector {
        layer: Layer,
        what: &'static str,
        length: usize,
    },
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Truncated { layer, where_ } => write!(
                f,
                "truncated input at layer {} (while reading {})",
                layer.as_str(),
                where_.as_str()
            ),
            ParseError::BadContentType(t) => {
                write!(f, "record content_type {t} is outside the accepted subset (20/21/22/23)")
            }
            ParseError::InvalidRecordVersion { major, minor } => write!(
                f,
                "record legacy_version {major}.{minor} is not a supported TLS record version (major must be 3)"
            ),
            ParseError::RecordTooLarge { length, max } => {
                write!(f, "record fragment {length} exceeds cap {max}")
            }
            ParseError::HandshakeTooLarge { length, max } => {
                write!(f, "handshake message {length} exceeds reassembly cap {max}")
            }
            ParseError::UnsupportedHandshakeType(t) => {
                write!(f, "handshake_type {t} is not accepted in the observer subset")
            }
            ParseError::InvalidClientHelloVersion { major, minor } => write!(
                f,
                "ClientHello client_version {major}.{minor} is not an accepted legacy TLS version"
            ),
            ParseError::LengthMismatch {
                layer,
                kind,
                what,
                declared,
                actual,
            } => write!(
                f,
                "{} length mismatch in {} ({}): declared {}, actual {}",
                kind.as_str(),
                layer.as_str(),
                what,
                declared,
                actual
            ),
            ParseError::MalformedVector { layer, what, length } => write!(
                f,
                "malformed vector {} in {}: invalid length {}",
                what,
                layer.as_str(),
                length
            ),
        }
    }
}

impl std::error::Error for ParseError {}

/// Non-fatal observations kept in the report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Warning {
    /// A handshake message other than ClientHello appeared (ServerHello, ...).
    /// A real client stream never contains one; the observer skips it rather
    /// than guessing, and records what it saw.
    SkippedHandshake { type_: u8 },
    /// Two extensions carried the same `extension_type`. The first occurrence
    /// wins; the repeat is recorded. RFC 8446 forbids this for most types.
    DuplicateExtension { type_: u16 },
    /// A GREASE extension_type was seen and classified, not treated as unknown.
    GreaseExtension { type_: u16 },
    /// A GREASE cipher-suite value was seen and classified.
    GreaseCipherSuite { value: u16 },
    /// server_name list contained a name type outside the accepted subset
    /// (only host_name = 0 is parsed).
    UnknownServerNameType { type_: u8 },
    /// The SNI host_name was not valid UTF-8 and was dropped.
    InvalidSniUtf8,
    /// An ALPN ProtocolName was empty or otherwise invalid; it was skipped.
    InvalidAlpnEntry,
}

impl fmt::Display for Warning {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Warning::SkippedHandshake { type_ } => {
                write!(f, "skipped non-ClientHello handshake message type {type_}")
            }
            Warning::DuplicateExtension { type_ } => {
                write!(
                    f,
                    "duplicate extension_type {type_:#06x} ignored (first wins)"
                )
            }
            Warning::GreaseExtension { type_ } => {
                write!(f, "GREASE extension {type_:#06x} classified")
            }
            Warning::GreaseCipherSuite { value } => {
                write!(f, "GREASE cipher_suite {value:#06x} classified")
            }
            Warning::UnknownServerNameType { type_ } => {
                write!(
                    f,
                    "server_name entry type {type_} is not host_name (0); skipped"
                )
            }
            Warning::InvalidSniUtf8 => f.write_str("SNI host_name was not valid UTF-8; dropped"),
            Warning::InvalidAlpnEntry => f.write_str("ALPN entry was empty or malformed; skipped"),
        }
    }
}
