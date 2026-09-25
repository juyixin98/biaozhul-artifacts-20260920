//! Incremental, byte-wise HTTP/1.1 request framer.
//!
//! The framer is a hand-written state machine: feed it arbitrary byte slices
//! via [`Framer::step`], and each call either yields a fully delimited
//! [`RequestFrame`], asks for more bytes, or rejects the connection with a
//! classified [`ParseError`]. No off-the-shelf HTTP parser is used.
//!
//! Supported subset (see `README.md`):
//! * request line exactly `METHOD SP target SP HTTP/1.1 CRLF`;
//! * header fields per RFC 7230 token/value syntax, CRLF line endings only;
//! * message body delimited by exactly one of:
//!     - a single valid `Content-Length` (fixed length),
//!     - `Transfer-Encoding: chunked` (with optional trailers/extensions),
//!     - no body (neither field present);
//! * HTTP/1.1 pipelining: after a frame, the next request begins immediately.
//!
//! Rejected (never silently normalised):
//! * conflicting/duplicate `Content-Length`;
//! * `Transfer-Encoding` together with `Content-Length`;
//! * TE value other than the single coding `chunked`;
//! * bare LF / bare CR, obs-fold, ambiguous whitespace before `:`;
//! * every declared length limit.

use crate::error::{ErrorKind, ParseError};
use crate::text::{self, eq_ignore_ascii_case, is_tchar, trim_ows};

/// Upper bounds enforced by the framer. Every rejection has a distinct
/// [`ErrorKind`] so callers can tell ambiguity attacks from limit cuts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Bytes in the request line, excluding CRLF.
    pub max_request_line_len: usize,
    /// Total bytes of all header lines (content + CRLF).
    pub max_header_section_bytes: usize,
    /// Maximum number of header fields.
    pub max_header_count: usize,
    /// Maximum declared/transferred body size in bytes.
    pub max_body_bytes: u64,
    /// Bytes of one chunk-size line (size + extensions), excluding CRLF.
    pub max_chunk_size_line: usize,
    /// Total bytes of the trailer section.
    pub max_trailer_section_bytes: usize,
    /// Maximum number of trailer fields.
    pub max_trailer_count: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_request_line_len: 8 * 1024,
            max_header_section_bytes: 64 * 1024,
            max_header_count: 100,
            max_body_bytes: 1024 * 1024,
            max_chunk_size_line: 1024,
            max_trailer_section_bytes: 16 * 1024,
            max_trailer_count: 16,
        }
    }
}

/// One header field (name and value are stored trimmed of OWS).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub name: Vec<u8>,
    pub value: Vec<u8>,
}

impl Header {
    pub fn new(name: &[u8], value: &[u8]) -> Self {
        Header {
            name: name.to_vec(),
            value: value.to_vec(),
        }
    }

    /// ASCII case-insensitive name comparison.
    pub fn is(&self, name: &str) -> bool {
        eq_ignore_ascii_case(&self.name, name.as_bytes())
    }
}

/// How the message body of a frame was delimited.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Framing {
    /// No transfer-related header: body is empty.
    None,
    /// `Content-Length: n`.
    FixedLen(u64),
    /// `Transfer-Encoding: chunked`.
    Chunked,
}

/// A fully delimited HTTP/1.1 request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestFrame {
    pub method: Vec<u8>,
    pub target: Vec<u8>,
    pub headers: Vec<Header>,
    pub framing: Framing,
    pub body: Vec<u8>,
    pub trailers: Vec<Header>,
    /// Bytes consumed from the stream by exactly this frame.
    pub consumed: usize,
    /// Whether the framer would accept another request on this connection
    /// (false when `Connection: close` was seen).
    pub keep_alive: bool,
}

/// Result of feeding bytes into [`Framer::step`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    /// More bytes are needed before the current frame can be delimited.
    Incomplete,
    /// One complete request frame was parsed; feed the remaining bytes again.
    Frame(RequestFrame),
    /// The stream violates the supported subset; the framer is poisoned.
    Error(ParseError),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Start,
    Headers,
    FixedBody,
    ChunkSize,
    ChunkData,
    ChunkCr,
    ChunkLf,
    Trailer,
    Poisoned,
}

/// Incremental framer. One instance per TCP connection.
#[derive(Debug)]
pub struct Framer {
    limits: Limits,
    state: State,

    /// Absolute stream position of the next byte handed to `step`.
    bytes_total: usize,
    /// Absolute stream position where the current frame started.
    frame_start: usize,

    // pending-line accumulator shared by all line-reading states
    line: Vec<u8>,
    line_cr: bool,
    cr_offset: usize,
    /// Absolute offset where the line currently in `line` began. Set when the
    /// accumulator is empty (first byte of a new line) and therefore stable
    /// across chunk boundaries.
    pending_line_start: usize,

    // per-frame state
    method: Vec<u8>,
    target: Vec<u8>,
    headers: Vec<Header>,
    /// Absolute offset of each header line, parallel to `headers`.
    header_offsets: Vec<usize>,
    framing: Framing,
    keep_alive: bool,
    fixed_remaining: u64,
    body: Vec<u8>,
    body_total: u64,
    chunk_remaining: u64,
    trailers: Vec<Header>,
    header_section_bytes: usize,
    trailer_section_bytes: usize,
}

impl Default for Framer {
    fn default() -> Self {
        Self::new()
    }
}

impl Framer {
    pub fn new() -> Self {
        Self::with_limits(Limits::default())
    }

    pub fn with_limits(limits: Limits) -> Self {
        Framer {
            limits,
            state: State::Start,
            bytes_total: 0,
            frame_start: 0,
            line: Vec::new(),
            line_cr: false,
            cr_offset: 0,
            pending_line_start: 0,
            method: Vec::new(),
            target: Vec::new(),
            headers: Vec::new(),
            header_offsets: Vec::new(),
            framing: Framing::None,
            keep_alive: true,
            fixed_remaining: 0,
            body: Vec::new(),
            body_total: 0,
            chunk_remaining: 0,
            trailers: Vec::new(),
            header_section_bytes: 0,
            trailer_section_bytes: 0,
        }
    }

    /// Total bytes consumed from the stream so far.
    pub fn position(&self) -> usize {
        self.bytes_total
    }

    /// Feed bytes; returns the outcome together with how many of `input`'s
    /// leading bytes were consumed. On [`Step::Incomplete`] those bytes are
    /// absorbed into the framer; on [`Step::Frame`] the remainder belongs to
    /// the next pipelined request; on [`Step::Error`] the connection is dead.
    pub fn step(&mut self, input: &[u8]) -> (Step, usize) {
        if self.state == State::Poisoned {
            return (
                Step::Error(ParseError::new(ErrorKind::Poisoned, self.bytes_total)),
                0,
            );
        }

        let mut rest = input;
        let mut used_total = 0;

        loop {
            let (outcome, used) = self.dispatch(rest);
            used_total += used;
            rest = &rest[used..];
            match outcome {
                StepOut::Continue => {
                    if rest.is_empty() {
                        return (Step::Incomplete, used_total);
                    }
                }
                StepOut::Incomplete => return (Step::Incomplete, used_total),
                StepOut::Frame(frame) => return (Step::Frame(frame), used_total),
                StepOut::Error(err) => {
                    self.state = State::Poisoned;
                    return (Step::Error(err), used_total);
                }
            }
        }
    }

    fn dispatch(&mut self, input: &[u8]) -> (StepOut, usize) {
        match self.state {
            State::Start => self.parse_request_line(input),
            State::Headers => self.parse_headers(input),
            State::FixedBody => self.parse_fixed_body(input),
            State::ChunkSize => self.parse_chunk_size(input),
            State::ChunkData => self.parse_chunk_data(input),
            State::ChunkCr => self.expect_cr(input),
            State::ChunkLf => self.expect_lf(input, State::ChunkSize),
            State::Trailer => self.parse_trailer(input),
            State::Poisoned => (
                StepOut::Error(ParseError::new(ErrorKind::Poisoned, self.bytes_total)),
                0,
            ),
        }
    }

    // ------------------------------------------------------------------
    // line reader
    // ------------------------------------------------------------------

    /// Accumulate one CRLF-terminated line into `self.line`.
    ///
    /// Bare LF is rejected at the LF byte; a CR not followed by LF is rejected
    /// at the CR byte. `max_content` bounds the content length (no CRLF);
    /// overflow is reported as `overflow_kind`.
    fn read_line(
        &mut self,
        input: &[u8],
        max_content: usize,
        overflow_kind: ErrorKind,
    ) -> (LineResult, usize) {
        let mut i = 0usize;

        if self.line_cr {
            if input.is_empty() {
                return (LineResult::NeedMore, 0);
            }
            let off = self.cr_offset;
            let b = input[0];
            self.bytes_total += 1;
            self.line_cr = false;
            if b == b'\n' {
                let line = std::mem::take(&mut self.line);
                return (LineResult::Complete(line), 1);
            } else {
                return (
                    LineResult::Err(ParseError::new(ErrorKind::BareCarriageReturn, off)),
                    1,
                );
            }
        }

        if self.line.is_empty() {
            self.pending_line_start = self.bytes_total;
        }

        while i < input.len() {
            let b = input[i];
            let abs = self.bytes_total + i;
            if b == b'\n' {
                return (
                    LineResult::Err(ParseError::new(ErrorKind::BareLineFeed, abs)),
                    i + 1,
                );
            } else if b == b'\r' {
                self.line_cr = true;
                self.cr_offset = abs;
                i += 1;
                if i == input.len() {
                    self.bytes_total += i;
                    return (LineResult::NeedMore, i);
                }
                self.bytes_total += i + 1;
                if input[i] == b'\n' {
                    self.line_cr = false;
                    let line = std::mem::take(&mut self.line);
                    return (LineResult::Complete(line), i + 1);
                } else {
                    return (
                        LineResult::Err(ParseError::new(
                            ErrorKind::BareCarriageReturn,
                            self.cr_offset,
                        )),
                        i + 1,
                    );
                }
            } else {
                if self.line.len() >= max_content {
                    return (LineResult::Err(ParseError::new(overflow_kind, abs)), i);
                }
                self.line.push(b);
                i += 1;
            }
        }

        self.bytes_total += i;
        (LineResult::NeedMore, i)
    }

    // ------------------------------------------------------------------
    // request line
    // ------------------------------------------------------------------

    fn parse_request_line(&mut self, input: &[u8]) -> (StepOut, usize) {
        let (res, used) = self.read_line(
            input,
            self.limits.max_request_line_len,
            ErrorKind::RequestLineTooLong,
        );
        match res {
            LineResult::NeedMore => (StepOut::Incomplete, used),
            LineResult::Err(err) => (StepOut::Error(err), used),
            LineResult::Complete(line) => {
                let line_start = self.pending_line_start;
                match self.accept_request_line(&line, line_start) {
                    Ok(()) => {
                        self.state = State::Headers;
                        (StepOut::Continue, used)
                    }
                    Err(err) => (StepOut::Error(err), used),
                }
            }
        }
    }

    fn accept_request_line(&mut self, line: &[u8], line_start: usize) -> Result<(), ParseError> {
        // Exactly three SP-separated tokens; tabs never legal; no leading
        // whitespace (an empty physical line before a request is refused).
        if line.is_empty() || line.contains(&b'\t') {
            return Err(ParseError::new(ErrorKind::MalformedRequestLine, line_start));
        }
        let mut parts = line.splitn(4, |b| *b == b' ');
        let method = parts.next().unwrap_or_default();
        let target = parts.next();
        let version = parts.next();
        let extra = parts.next();

        let target = target.filter(|t| !t.is_empty());
        let version = version.filter(|t| !t.is_empty());

        if method.is_empty() || target.is_none() || version.is_none() || extra.is_some() {
            return Err(ParseError::new(ErrorKind::MalformedRequestLine, line_start));
        }
        let target = target.unwrap();
        let version = version.unwrap();

        if !method.iter().all(|b| is_tchar(*b)) {
            return Err(ParseError::new(ErrorKind::InvalidMethod, line_start));
        }
        if !target.iter().all(|b| text::is_request_target_byte(*b)) {
            return Err(ParseError::new(ErrorKind::InvalidRequestTarget, line_start));
        }
        if version != b"HTTP/1.1" {
            return Err(ParseError::new(ErrorKind::UnsupportedVersion, line_start));
        }

        self.method = method.to_vec();
        self.target = target.to_vec();
        Ok(())
    }

    // ------------------------------------------------------------------
    // headers
    // ------------------------------------------------------------------

    fn parse_headers(&mut self, input: &[u8]) -> (StepOut, usize) {
        let (res, used) = self.read_line(
            input,
            self.limits.max_header_section_bytes,
            ErrorKind::HeaderSectionTooLarge,
        );
        match res {
            LineResult::NeedMore => (StepOut::Incomplete, used),
            LineResult::Err(err) => (StepOut::Error(err), used),
            LineResult::Complete(line) => {
                let line_start = self.pending_line_start;
                self.header_section_bytes += line.len() + 2;
                if self.header_section_bytes > self.limits.max_header_section_bytes {
                    return (
                        StepOut::Error(ParseError::new(
                            ErrorKind::HeaderSectionTooLarge,
                            line_start,
                        )),
                        used,
                    );
                }

                // Empty line: header section finished.
                if line.is_empty() {
                    let offsets = std::mem::take(&mut self.header_offsets);
                    return match self.finish_headers(&offsets) {
                        Ok(out) => (out, used),
                        Err(err) => (StepOut::Error(err), used),
                    };
                }

                if self.headers.len() >= self.limits.max_header_count {
                    return (
                        StepOut::Error(ParseError::new(ErrorKind::TooManyHeaders, line_start)),
                        used,
                    );
                }

                match parse_header_line(&line) {
                    Ok(header) => {
                        self.headers.push(header);
                        self.header_offsets.push(line_start);
                        (StepOut::Continue, used)
                    }
                    Err(kind) => (StepOut::Error(ParseError::new(kind, line_start)), used),
                }
            }
        }
    }

    fn finish_headers(&mut self, offsets: &[usize]) -> Result<StepOut, ParseError> {
        let mut cl: Option<(usize, u64)> = None;
        let mut te_offsets: Vec<usize> = Vec::new();
        let mut te_bad_offset: Option<usize> = None;

        for (idx, h) in self.headers.iter().enumerate() {
            let off = offsets[idx];
            if h.is("content-length") {
                if let Some((prev_off, _)) = cl {
                    // Two distinct CL header fields — always rejected.
                    return Err(ParseError::new(
                        ErrorKind::DuplicateContentLength,
                        std::cmp::min(prev_off, off),
                    ));
                }
                match parse_content_length(&h.value) {
                    Some(Ok(n)) => cl = Some((off, n)),
                    Some(Err(kind)) => return Err(ParseError::new(kind, off)),
                    None => return Err(ParseError::new(ErrorKind::InvalidContentLength, off)),
                }
            } else if h.is("transfer-encoding") {
                te_offsets.push(off);
                if !eq_ignore_ascii_case(trim_ows(&h.value), b"chunked") {
                    te_bad_offset = Some(off);
                }
            }
        }

        if let Some(off) = te_bad_offset {
            return Err(ParseError::new(ErrorKind::InvalidTransferEncoding, off));
        }
        if te_offsets.len() > 1 {
            return Err(ParseError::new(
                ErrorKind::InvalidTransferEncoding,
                te_offsets[1],
            ));
        }

        self.keep_alive = !connection_requests_close(&self.headers);

        if te_offsets.first().copied().is_some() {
            if let Some((cl_off, _)) = cl {
                return Err(ParseError::new(ErrorKind::TeAndCl, cl_off));
            }
            self.framing = Framing::Chunked;
            self.state = State::ChunkSize;
            Ok(StepOut::Continue)
        } else if let Some((off, n)) = cl {
            if n > self.limits.max_body_bytes {
                return Err(ParseError::new(ErrorKind::BodyTooLarge, off));
            }
            self.framing = Framing::FixedLen(n);
            self.fixed_remaining = n;
            if n == 0 {
                Ok(StepOut::Frame(self.complete_frame()))
            } else {
                self.state = State::FixedBody;
                Ok(StepOut::Continue)
            }
        } else {
            self.framing = Framing::None;
            Ok(StepOut::Frame(self.complete_frame()))
        }
    }

    // ------------------------------------------------------------------
    // fixed-length body
    // ------------------------------------------------------------------

    fn parse_fixed_body(&mut self, input: &[u8]) -> (StepOut, usize) {
        if input.is_empty() {
            return (StepOut::Incomplete, 0);
        }
        let want = std::cmp::min(self.fixed_remaining as usize, input.len());
        self.body.extend_from_slice(&input[..want]);
        self.body_total += want as u64;
        self.fixed_remaining -= want as u64;
        self.bytes_total += want;
        if self.fixed_remaining == 0 {
            let frame = self.complete_frame();
            (StepOut::Frame(frame), want)
        } else {
            (StepOut::Incomplete, want)
        }
    }

    // ------------------------------------------------------------------
    // chunked
    // ------------------------------------------------------------------

    fn parse_chunk_size(&mut self, input: &[u8]) -> (StepOut, usize) {
        let (res, used) = self.read_line(
            input,
            self.limits.max_chunk_size_line,
            ErrorKind::MalformedChunkSize,
        );
        match res {
            LineResult::NeedMore => (StepOut::Incomplete, used),
            LineResult::Err(err) => (StepOut::Error(err), used),
            LineResult::Complete(line) => {
                let line_start = self.pending_line_start;
                match parse_chunk_line(&line, self.limits, self.body_total, line_start) {
                    Ok(0) => {
                        self.chunk_remaining = 0;
                        self.state = State::Trailer;
                        (StepOut::Continue, used)
                    }
                    Ok(size) => {
                        self.chunk_remaining = size;
                        self.state = State::ChunkData;
                        (StepOut::Continue, used)
                    }
                    Err(err) => (StepOut::Error(err), used),
                }
            }
        }
    }

    fn parse_chunk_data(&mut self, input: &[u8]) -> (StepOut, usize) {
        if input.is_empty() {
            return (StepOut::Incomplete, 0);
        }
        let want = std::cmp::min(self.chunk_remaining as usize, input.len());
        self.body.extend_from_slice(&input[..want]);
        self.body_total += want as u64;
        self.chunk_remaining -= want as u64;
        self.bytes_total += want;
        if self.chunk_remaining == 0 {
            self.state = State::ChunkCr;
            (StepOut::Continue, want)
        } else {
            (StepOut::Incomplete, want)
        }
    }

    fn expect_cr(&mut self, input: &[u8]) -> (StepOut, usize) {
        if input.is_empty() {
            return (StepOut::Incomplete, 0);
        }
        let abs = self.bytes_total;
        self.bytes_total += 1;
        if input[0] == b'\r' {
            self.state = State::ChunkLf;
            (StepOut::Continue, 1)
        } else {
            (
                StepOut::Error(ParseError::new(ErrorKind::MalformedChunkSize, abs)),
                1,
            )
        }
    }

    fn expect_lf(&mut self, input: &[u8], next: State) -> (StepOut, usize) {
        if input.is_empty() {
            return (StepOut::Incomplete, 0);
        }
        let abs = self.bytes_total;
        self.bytes_total += 1;
        if input[0] == b'\n' {
            self.state = next;
            (StepOut::Continue, 1)
        } else {
            (
                StepOut::Error(ParseError::new(ErrorKind::MalformedChunkSize, abs)),
                1,
            )
        }
    }

    // ------------------------------------------------------------------
    // trailers
    // ------------------------------------------------------------------

    fn parse_trailer(&mut self, input: &[u8]) -> (StepOut, usize) {
        let (res, used) = self.read_line(
            input,
            self.limits.max_trailer_section_bytes,
            ErrorKind::TrailerSectionTooLarge,
        );
        match res {
            LineResult::NeedMore => (StepOut::Incomplete, used),
            LineResult::Err(err) => (StepOut::Error(err), used),
            LineResult::Complete(line) => {
                let line_start = self.pending_line_start;
                self.trailer_section_bytes += line.len() + 2;
                if self.trailer_section_bytes > self.limits.max_trailer_section_bytes {
                    return (
                        StepOut::Error(ParseError::new(
                            ErrorKind::TrailerSectionTooLarge,
                            line_start,
                        )),
                        used,
                    );
                }
                if line.is_empty() {
                    let frame = self.complete_frame();
                    return (StepOut::Frame(frame), used);
                }
                if self.trailers.len() >= self.limits.max_trailer_count {
                    return (
                        StepOut::Error(ParseError::new(ErrorKind::TooManyTrailers, line_start)),
                        used,
                    );
                }
                match parse_header_line(&line) {
                    Ok(header) => {
                        if header.is("content-length")
                            || header.is("transfer-encoding")
                            || header.is("trailer")
                        {
                            return (
                                StepOut::Error(ParseError::new(
                                    ErrorKind::ForbiddenTrailerField,
                                    line_start,
                                )),
                                used,
                            );
                        }
                        self.trailers.push(header);
                        (StepOut::Continue, used)
                    }
                    Err(kind) => (StepOut::Error(ParseError::new(kind, line_start)), used),
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // frame assembly / reset
    // ------------------------------------------------------------------

    fn complete_frame(&mut self) -> RequestFrame {
        let consumed = self.bytes_total - self.frame_start;
        let frame = RequestFrame {
            method: std::mem::take(&mut self.method),
            target: std::mem::take(&mut self.target),
            headers: std::mem::take(&mut self.headers),
            framing: self.framing,
            body: std::mem::take(&mut self.body),
            trailers: std::mem::take(&mut self.trailers),
            consumed,
            keep_alive: self.keep_alive,
        };

        // Reset per-frame state for the next pipelined request.
        self.framing = Framing::None;
        self.keep_alive = true;
        self.fixed_remaining = 0;
        self.body_total = 0;
        self.chunk_remaining = 0;
        self.header_section_bytes = 0;
        self.trailer_section_bytes = 0;
        self.header_offsets.clear();
        self.frame_start = self.bytes_total;
        self.state = State::Start;
        frame
    }
}

// internal helpers -------------------------------------------------------

#[derive(Debug)]
enum LineResult {
    NeedMore,
    Complete(Vec<u8>),
    Err(ParseError),
}

#[derive(Debug)]
enum StepOut {
    Continue,
    Incomplete,
    Frame(RequestFrame),
    Error(ParseError),
}

/// Parse one header line (no CRLF) under the strict subset.
fn parse_header_line(line: &[u8]) -> Result<Header, ErrorKind> {
    // A physical line beginning with SP/HTAB is an obsolete folded
    // continuation (obs-fold) — refused.
    if matches!(line.first(), Some(b' ') | Some(b'\t')) {
        return Err(ErrorKind::ObsFold);
    }
    let colon = line
        .iter()
        .position(|b| *b == b':')
        .ok_or(ErrorKind::MalformedHeader)?;
    let name = &line[..colon];
    if name.is_empty() || !name.iter().all(|b| is_tchar(*b)) {
        // Also covers trailing whitespace before ':' (SP/HTAB aren't tchar).
        return Err(ErrorKind::InvalidHeaderName);
    }
    let value = trim_ows(&line[colon + 1..]);
    if !value.iter().all(|b| text::is_field_value_byte(*b)) {
        return Err(ErrorKind::InvalidHeaderValue);
    }
    Ok(Header::new(name, value))
}

/// Parse a `Content-Length` field body.
///
/// Accepts the RFC 9112 comma-list form but requires every value to agree
/// (`Content-Length: 5, 5` is accepted; `5, 6` is a conflict). Distinct
/// repeated header fields are handled by the caller.
///
/// Returns:
/// * `None` — not a length (empty / non-digit) → invalid;
/// * `Some(Err(kind))` — numeric overflow or conflicting values;
/// * `Some(Ok(n))` — the length.
fn parse_content_length(value: &[u8]) -> Option<Result<u64, ErrorKind>> {
    let v = trim_ows(value);
    let mut agreed: Option<u64> = None;
    for item in v.split(|b| *b == b',') {
        let item = trim_ows(item);
        if item.is_empty() || !item.iter().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let mut n: u64 = 0;
        for b in item {
            let d = (*b - b'0') as u64;
            n = match n.checked_mul(10).and_then(|x| x.checked_add(d)) {
                Some(n) => n,
                None => return Some(Err(ErrorKind::InvalidContentLength)),
            };
        }
        match agreed {
            Some(a) if a != n => return Some(Err(ErrorKind::ConflictingContentLength)),
            Some(_) => {}
            None => agreed = Some(n),
        }
    }
    agreed.map(Ok)
}

/// Whether any `Connection` header lists the `close` token.
fn connection_requests_close(headers: &[Header]) -> bool {
    headers
        .iter()
        .filter(|h| h.is("connection"))
        .flat_map(|h| h.value.split(|b| *b == b',' || *b == b' ' || *b == b'\t'))
        .map(trim_ows)
        .any(|tok| eq_ignore_ascii_case(tok, b"close"))
}

/// Parse one chunk line: `1*HEXDIG [ chunk-ext ]`.
fn parse_chunk_line(
    line: &[u8],
    limits: Limits,
    body_total: u64,
    line_start: usize,
) -> Result<u64, ParseError> {
    let semi = line.iter().position(|b| *b == b';').unwrap_or(line.len());
    let hex = &line[..semi];
    if hex.is_empty() {
        return Err(ParseError::new(ErrorKind::MalformedChunkSize, line_start));
    }
    let mut size: u64 = 0;
    for b in hex {
        if !text::is_ascii_hexdig(*b) {
            return Err(ParseError::new(ErrorKind::MalformedChunkSize, line_start));
        }
        let d = (*b as char).to_digit(16).unwrap() as u64;
        size = match size.checked_mul(16).and_then(|x| x.checked_add(d)) {
            Some(v) => v,
            None => return Err(ParseError::new(ErrorKind::ChunkSizeOverflow, line_start)),
        };
    }
    if semi < line.len() {
        validate_chunk_extensions(&line[semi + 1..], line_start)?;
    }
    if body_total.saturating_add(size) > limits.max_body_bytes {
        return Err(ParseError::new(ErrorKind::ChunkSizeTooLarge, line_start));
    }
    Ok(size)
}

/// Validate the strict chunk-extension subset.
///
/// `rest` is everything after the first `;`, i.e. a list shaped like
/// `BWS name [ BWS "=" BWS ( token / quoted-string ) ] *( BWS ";" … ) BWS`.
fn validate_chunk_extensions(rest: &[u8], line_start: usize) -> Result<(), ParseError> {
    let err = || ParseError::new(ErrorKind::InvalidChunkExtension, line_start);

    fn skip_ows(p: &[u8]) -> &[u8] {
        let n = p
            .iter()
            .position(|b| *b != b' ' && *b != b'\t')
            .unwrap_or(p.len());
        &p[n..]
    }

    let mut p = skip_ows(rest);
    while !p.is_empty() {
        // chunk-ext-name = token
        let n = p.iter().position(|b| !is_tchar(*b)).unwrap_or(p.len());
        if n == 0 {
            return Err(err());
        }
        p = skip_ows(&p[n..]);
        if p.first() == Some(&b'=') {
            p = skip_ows(&p[1..]);
            if p.first() == Some(&b'"') {
                // quoted-string: "*( qdtext / quoted-pair )"
                p = &p[1..];
                loop {
                    match p.first() {
                        Some(b'"') => {
                            p = &p[1..];
                            break;
                        }
                        Some(b'\\') => {
                            if p.len() < 2 {
                                return Err(err());
                            }
                            let q = p[1];
                            if !(text::is_vchar(q) || text::is_obs_text(q)) {
                                return Err(err());
                            }
                            p = &p[2..];
                        }
                        Some(b)
                            if *b == b'\t'
                                || *b == b' '
                                || (0x23..=0x5B).contains(b)
                                || (0x5D..=0x7E).contains(b)
                                || text::is_obs_text(*b) =>
                        {
                            p = &p[1..];
                        }
                        _ => return Err(err()),
                    }
                }
            } else {
                let n = p.iter().position(|b| !is_tchar(*b)).unwrap_or(p.len());
                if n == 0 {
                    return Err(err());
                }
                p = &p[n..];
            }
        }
        p = skip_ows(p);
        match p.first() {
            None => return Ok(()),
            Some(b';') => p = skip_ows(&p[1..]),
            Some(_) => return Err(err()),
        }
    }
    // Empty after the ';' means the extension had no name.
    Err(err())
}
