//! Error taxonomy for the incremental framer.
//!
//! Every reject has:
//! * an [`ErrorKind`] — the classification required by the project spec;
//! * an absolute byte `offset` inside the byte stream — where in the stream
//!   the offending construct starts. The offset is independent of how the
//!   bytes are chunked across calls (see the byte-split tests).

use std::fmt;

/// All rejection types produced by this framer.
///
/// This list is intentionally explicit and finite: callers know exactly which
/// ambiguity or limit violation they are dealing with.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// The framer previously returned an error; it must not be reused.
    Poisoned,

    // ----- request line -----
    /// Line ending is `\n` without a preceding `\r`.
    BareLineFeed,
    /// A `\r` not followed by `\n` (and not waiting for the next byte).
    BareCarriageReturn,
    /// Request line does not have exactly `METHOD SP target SP HTTP-version`.
    MalformedRequestLine,
    /// Method token contains bytes outside RFC 7230 `tchar`.
    InvalidMethod,
    /// Request target contains SP/control bytes / non-visible bytes.
    InvalidRequestTarget,
    /// Version token is not the literal `HTTP/1.1` (older versions refused).
    UnsupportedVersion,

    // ----- header field syntax -----
    /// Header line contains no `:`.
    MalformedHeader,
    /// Header name is empty or contains bytes outside RFC 7230 `tchar`
    /// (this also covers a leading SP/HTAB, which used to be obs-fold).
    InvalidHeaderName,
    /// Header value contains control characters other than HTAB
    /// (covers bare CR/LF inside a value as well).
    InvalidHeaderValue,
    /// Obs-fold: a continuation line whose first byte is SP/HTAB.
    ObsFold,

    // ----- framing conflicts (request-smuggling ambiguities) -----
    /// More than one `Content-Length` header field.
    DuplicateContentLength,
    /// `Content-Length` value is not a valid non-negative decimal length.
    InvalidContentLength,
    /// `Content-Length` lists (or repeats) values that disagree.
    ConflictingContentLength,
    /// `Transfer-Encoding` and `Content-Length` are both present.
    TeAndCl,
    /// `Transfer-Encoding` is present but its value is not exactly
    /// `chunked` (multiple codings, `identity`, unknown codings, garbage…).
    InvalidTransferEncoding,

    // ----- chunked encoding -----
    /// Chunk-size line is not 1*HEXDIG [ BWS chunk-ext BWS ].
    MalformedChunkSize,
    /// Chunk-size hexadecimal value cannot fit in `u64`.
    ChunkSizeOverflow,
    /// Per-chunk size exceeds the configured limit.
    ChunkSizeTooLarge,
    /// Chunk extension contains bytes outside the strict subset
    /// (token chars, `;`, `=`, `"`, SP/HTAB at legal positions).
    InvalidChunkExtension,
    /// Trailer section contains a `Content-Length`/`Transfer-Encoding`/
    /// `Trailer` field.
    ForbiddenTrailerField,

    // ----- limits -----
    /// Request line exceeds the byte limit.
    RequestLineTooLong,
    /// Entire header section exceeds the byte limit.
    HeaderSectionTooLarge,
    /// Number of header fields exceeds the limit.
    TooManyHeaders,
    /// Declared / actual body exceeds the body byte limit.
    BodyTooLarge,
    /// Trailer section exceeds the byte limit.
    TrailerSectionTooLarge,
    /// Number of trailer fields exceeds the limit.
    TooManyTrailers,
}

impl ErrorKind {
    /// Map the framing error to the status code the test server emits.
    ///
    /// Syntactic violations are 400; smuggling ambiguities are 400 as well,
    /// but the body distinguishes them. Payload/limit violations are 413.
    /// No error is ever silently "normalised" into an accepted frame.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::BodyTooLarge
            | ErrorKind::ChunkSizeTooLarge
            | ErrorKind::ChunkSizeOverflow => 413,
            _ => 400,
        }
    }

    /// Stable machine-readable identifier used in JSON responses and docs.
    pub fn code(self) -> &'static str {
        match self {
            ErrorKind::Poisoned => "poisoned",
            ErrorKind::BareLineFeed => "bare_line_feed",
            ErrorKind::BareCarriageReturn => "bare_carriage_return",
            ErrorKind::MalformedRequestLine => "malformed_request_line",
            ErrorKind::InvalidMethod => "invalid_method",
            ErrorKind::InvalidRequestTarget => "invalid_request_target",
            ErrorKind::UnsupportedVersion => "unsupported_version",
            ErrorKind::MalformedHeader => "malformed_header",
            ErrorKind::InvalidHeaderName => "invalid_header_name",
            ErrorKind::InvalidHeaderValue => "invalid_header_value",
            ErrorKind::ObsFold => "obs_fold",
            ErrorKind::DuplicateContentLength => "duplicate_content_length",
            ErrorKind::InvalidContentLength => "invalid_content_length",
            ErrorKind::ConflictingContentLength => "conflicting_content_length",
            ErrorKind::TeAndCl => "te_and_cl",
            ErrorKind::InvalidTransferEncoding => "invalid_transfer_encoding",
            ErrorKind::MalformedChunkSize => "malformed_chunk_size",
            ErrorKind::ChunkSizeOverflow => "chunk_size_overflow",
            ErrorKind::ChunkSizeTooLarge => "chunk_size_too_large",
            ErrorKind::InvalidChunkExtension => "invalid_chunk_extension",
            ErrorKind::ForbiddenTrailerField => "forbidden_trailer_field",
            ErrorKind::RequestLineTooLong => "request_line_too_long",
            ErrorKind::HeaderSectionTooLarge => "header_section_too_large",
            ErrorKind::TooManyHeaders => "too_many_headers",
            ErrorKind::BodyTooLarge => "body_too_large",
            ErrorKind::TrailerSectionTooLarge => "trailer_section_too_large",
            ErrorKind::TooManyTrailers => "too_many_trailers",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}

impl std::error::Error for ErrorKind {}

/// A classified parse failure with an absolute byte offset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ParseError {
    pub kind: ErrorKind,
    /// Offset of the first byte involved in the error, relative to the start
    /// of the connection stream.
    pub offset: usize,
}

impl ParseError {
    pub fn new(kind: ErrorKind, offset: usize) -> Self {
        ParseError { kind, offset }
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte offset {}", self.kind.code(), self.offset)
    }
}

impl std::error::Error for ParseError {}
