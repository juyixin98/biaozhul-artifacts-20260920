//! Error types for the incremental HTTP/1.1 request framer.
//!
//! Every rejected byte stream produces a [`FrameError`] with a stable
//! [`ErrorKind`]. The kinds deliberately distinguish the classic
//! request-smuggling ambiguity cases (conflicting lengths, TE+CL
//! coexistence, bad line folding, ambiguous whitespace) from ordinary
//! syntax errors so tests can assert on them precisely.

use std::fmt;

/// Categorised framing error.
///
/// The discriminant names are part of the test surface: the split tests
/// assert that the *same* kind is produced regardless of how the input
/// bytes are fed to the parser.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum ErrorKind {
    /// Bare `\n` used as a line ending instead of `\r\n`.
    BadLineEnding,
    /// A request line/header line was split into the wrong number of
    /// tokens (e.g. `"GET /"` or `"GET / HTTP/1.1 extra"`).
    MalformedRequestLine,
    /// Method token is empty or contains a character outside `tchar`.
    InvalidMethod,
    /// Request target is empty, contains whitespace/control chars, or
    /// does not begin with `/` (`OPTIONS *` excepted).
    InvalidTarget,
    /// Version is not exactly `HTTP/1.1` (older/newer versions are out
    /// of the supported subset).
    UnsupportedVersion,
    /// Header field line has no `:` separator.
    HeaderMissingColon,
    /// Header field name is empty or contains an illegal character
    /// (including whitespace on the name side of the colon).
    InvalidHeaderName,
    /// Header field value contains a control character or non-ASCII
    /// byte, or non-collapsible whitespace was sent around it.
    InvalidHeaderValue,
    /// Obs-fold (`CRLF SP/HT` inside a header field) was observed.
    ObsoleteLineFolding,
    /// Whitespace in a place the grammar forbids: SP/HT between a
    /// header name and `:`, leading/trailing OWS around a field value
    /// (more than the single optional SP), or repeated SP in the
    /// request line. Rejecting these removes the spaces that
    /// historically desynchronise parsers.
    AmbiguousWhitespace,
    /// Two `Content-Length` header fields were present.
    DuplicateContentLength,
    /// `Content-Length` value was not a strict decimal length
    /// (empty, non-digit, leading zero, or overflowing `u64`).
    InvalidContentLength,
    /// A `Transfer-Encoding` field whose value was not exactly
    /// `chunked` (empty value, multiple codings, non-chunked coding…).
    InvalidTransferEncoding,
    /// Both `Transfer-Encoding` and `Content-Length` were present.
    /// RFC 9112 §6.3 requires rejection; this is the CL.TE/TE.CL
    /// smuggling vector.
    TeWithContentLength,
    /// Chunk-size line could not be parsed: empty, non-hex, or carried
    /// a chunk extension (`;…`).
    ChunkSizeInvalid,
    /// One chunk claimed a size above `max_chunk_size`.
    ChunkTooLarge,
    /// A chunk-body block was not terminated by `CRLF`.
    ChunkTerminator,
    /// A forbidden field (`Content-Length`/`Transfer-Encoding`)
    /// appeared in the trailer section.
    TrailerForbiddenField,
    /// Request line exceeded `max_request_line_bytes`.
    RequestLineTooLarge,
    /// Header block exceeded `max_header_block_bytes` (or
    /// `max_header_count` fields).
    HeadersTooLarge,
    /// Declared/decoded body exceeded `max_body_bytes`.
    BodyTooLarge,
    /// The stream ended while a request was still incomplete.
    Incomplete,
}

impl ErrorKind {
    /// HTTP status code the reference server maps this error to.
    /// 431/413/400/408 only; no response is generated for errors that
    /// the server itself cannot safely frame.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::RequestLineTooLarge
            | ErrorKind::HeadersTooLarge => 431,
            ErrorKind::BodyTooLarge | ErrorKind::ChunkTooLarge => 413,
            ErrorKind::Incomplete => 400,
            _ => 400,
        }
    }

    /// Short stable identifier, e.g. used in the
    /// `X-Frame-Error` response header.
    pub fn name(self) -> &'static str {
        match self {
            ErrorKind::BadLineEnding => "bad-line-ending",
            ErrorKind::MalformedRequestLine => "malformed-request-line",
            ErrorKind::InvalidMethod => "invalid-method",
            ErrorKind::InvalidTarget => "invalid-target",
            ErrorKind::UnsupportedVersion => "unsupported-version",
            ErrorKind::HeaderMissingColon => "header-missing-colon",
            ErrorKind::InvalidHeaderName => "invalid-header-name",
            ErrorKind::InvalidHeaderValue => "invalid-header-value",
            ErrorKind::ObsoleteLineFolding => "obsolete-line-folding",
            ErrorKind::AmbiguousWhitespace => "ambiguous-whitespace",
            ErrorKind::DuplicateContentLength => "duplicate-content-length",
            ErrorKind::InvalidContentLength => "invalid-content-length",
            ErrorKind::InvalidTransferEncoding => "invalid-transfer-encoding",
            ErrorKind::TeWithContentLength => "te-with-content-length",
            ErrorKind::ChunkSizeInvalid => "chunk-size-invalid",
            ErrorKind::ChunkTooLarge => "chunk-too-large",
            ErrorKind::ChunkTerminator => "chunk-terminator",
            ErrorKind::TrailerForbiddenField => "trailer-forbidden-field",
            ErrorKind::RequestLineTooLarge => "request-line-too-large",
            ErrorKind::HeadersTooLarge => "headers-too-large",
            ErrorKind::BodyTooLarge => "body-too-large",
            ErrorKind::Incomplete => "incomplete",
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name())
    }
}

/// A framing failure at a byte offset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FrameError {
    /// What went wrong.
    pub kind: ErrorKind,
    /// Offset within the *current request* where the error was
    /// detected. Offsets are best-effort diagnostics: tests assert on
    /// `kind`, never on offset, because a stream fed in smaller pieces
    /// may report the same violation at a different buffered offset.
    pub offset: usize,
}

impl FrameError {
    pub fn new(kind: ErrorKind, offset: usize) -> Self {
        Self { kind, offset }
    }
}

impl fmt::Display for FrameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte {}", self.kind.name(), self.offset)
    }
}

impl std::error::Error for FrameError {}
