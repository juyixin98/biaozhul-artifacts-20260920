//! Incremental HTTP/1.1 request framer.
//!
//! [`Framer`] accepts arbitrary byte slices via [`Framer::feed`] and
//! yields every complete request contained in the stream. Feeding one
//! byte at a time, feeding the whole stream at once, or splitting at
//! any byte boundary MUST produce the same sequence of [`Request`]s (or
//! the same [`ErrorKind`]) — this is the property the split tests pin.
//!
//! The state machine is genuinely cross-feed (not "buffer it all then
//! parse"): head scanning, fixed-length bodies and every chunked
//! phase remember their positions between calls.

use crate::error::{ErrorKind, FrameError};
use crate::parser::{self, Framing, Header, Request};

/// Configurable size/number limits. All are checked during streaming
/// so oversized senders are rejected without being buffered in full.
#[derive(Debug, Clone)]
pub struct Limits {
    pub max_request_line_bytes: usize,
    pub max_header_block_bytes: usize,
    pub max_header_count: usize,
    pub max_body_bytes: u64,
    pub max_chunk_size: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_request_line_bytes: 8 * 1024,
            max_header_block_bytes: 64 * 1024,
            max_header_count: 100,
            max_body_bytes: 1024 * 1024,
            max_chunk_size: 1024 * 1024,
        }
    }
}

/// In-progress head (request line + header block).
#[derive(Debug, Clone)]
struct HeadState {
    /// Absolute index of the first byte of this request in `buf`.
    req_start: usize,
    /// Start of the line currently accumulating.
    line_start: usize,
    /// Scan cursor; the prefix before it is known to contain no
    /// complete line.
    scan: usize,
    method: Option<String>,
    target: Option<String>,
    headers: Vec<Header>,
    cl: Option<u64>,
    cl_seen: bool,
    te_seen: bool,
}

impl HeadState {
    fn new(req_start: usize) -> Self {
        HeadState {
            req_start,
            line_start: req_start,
            scan: req_start,
            method: None,
            target: None,
            headers: Vec::new(),
            cl: None,
            cl_seen: false,
            te_seen: false,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ChunkPhase {
    SizeLine,
    Data,
    CrlfAfterData,
    Trailer,
}

/// In-progress chunked body.
#[derive(Debug, Clone)]
struct ChunkedState {
    /// Absolute index where the current region begins (a size line,
    /// a chunk-data block, or a trailer line).
    region_start: usize,
    /// Scan cursor for terminator-searching phases.
    scan: usize,
    phase: ChunkPhase,
    /// Size of the chunk currently being read.
    chunk_size: u64,
    /// Sum of decoded chunk-data bytes seen so far.
    total: u64,
    body: Vec<u8>,
    method: String,
    target: String,
    headers: Vec<Header>,
    trailers: Vec<Header>,
}

/// Fixed-length (or absent) body in progress.
#[derive(Debug, Clone)]
struct FixedState {
    /// Absolute index of the first *not yet consumed* body byte.
    body_start: usize,
    remaining: u64,
    method: String,
    target: String,
    headers: Vec<Header>,
    framing: Framing,
    body: Vec<u8>,
}

#[derive(Debug, Clone)]
enum Machine {
    Head(HeadState),
    Fixed(FixedState),
    Chunked(ChunkedState),
}

/// Result of advancing one machine as far as the buffered bytes allow.
enum Step {
    /// More bytes required; machine is returned for the next feed.
    Incomplete(Machine),
    /// Head ended and the body machine has taken over (it may already
    /// make progress on the same buffer; the driver loops).
    Transition(Machine),
    /// A request finished at absolute index `end`; `next` is the
    /// machine for the following pipelined request.
    Frame {
        request: Request,
        end: usize,
        next: Machine,
    },
    /// Definitive framing failure.
    Error(Machine, FrameError),
}

/// The streaming framer.
#[derive(Debug, Clone)]
pub struct Framer {
    limits: Limits,
    buf: Vec<u8>,
    machine: Machine,
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
            buf: Vec::new(),
            machine: Machine::Head(HeadState::new(0)),
        }
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// Bytes of an unfinished request currently held.
    pub fn pending_bytes(&self) -> usize {
        self.buf.len()
    }

    /// Append bytes and return every request that completes as a
    /// result; one call can surface a whole pipelined batch.
    pub fn feed(&mut self, input: &[u8]) -> Result<Vec<Request>, FrameError> {
        self.buf.extend_from_slice(input);
        let mut out = Vec::new();
        loop {
            let available = self.buf.len();
            let machine =
                std::mem::replace(&mut self.machine, Machine::Head(HeadState::new(0)));
            match pump(machine, &self.buf, available, &self.limits) {
                Step::Incomplete(m) => {
                    self.machine = m;
                    break;
                }
                Step::Transition(m) => {
                    // Head finished but the body machine can make
                    // progress on bytes already buffered: keep pumping.
                    self.machine = m;
                }
                Step::Error(m, e) => {
                    self.machine = m;
                    return Err(e);
                }
                Step::Frame {
                    request,
                    end,
                    next,
                } => {
                    out.push(request);
                    if end >= self.buf.len() {
                        self.buf.clear();
                    } else {
                        self.buf.drain(..end);
                    }
                    self.machine = rebase(next, end);
                    if self.buf.is_empty() {
                        break;
                    }
                    // Otherwise loop: the new head machine may already
                    // contain a complete pipelined request.
                }
            }
        }
        Ok(out)
    }

    /// Signal end of stream; errors if a request is still partial.
    pub fn end_input(&mut self) -> Result<(), FrameError> {
        if self.buf.is_empty() {
            Ok(())
        } else {
            Err(FrameError::new(ErrorKind::Incomplete, 0))
        }
    }
}

/// Shift every absolute index in a freshly-installed machine back by
/// `base` after draining `[0, base)`.
fn rebase(mut m: Machine, base: usize) -> Machine {
    if base == 0 {
        return m;
    }
    match &mut m {
        Machine::Head(h) => {
            h.req_start = h.req_start.saturating_sub(base);
            h.line_start = h.line_start.saturating_sub(base);
            h.scan = h.scan.saturating_sub(base);
        }
        Machine::Fixed(f) => {
            f.body_start = f.body_start.saturating_sub(base);
        }
        Machine::Chunked(c) => {
            c.region_start = c.region_start.saturating_sub(base);
            c.scan = c.scan.saturating_sub(base);
        }
    }
    m
}

fn pump(m: Machine, buf: &[u8], available: usize, lim: &Limits) -> Step {
    match m {
        Machine::Head(h) => pump_head(h, buf, available, lim),
        Machine::Fixed(f) => pump_fixed(f, buf, available),
        Machine::Chunked(c) => pump_chunked(c, buf, available, lim),
    }
}

// ---------------------------------------------------------------------------
// Head
// ---------------------------------------------------------------------------

fn pump_head(mut h: HeadState, buf: &[u8], available: usize, lim: &Limits) -> Step {
    loop {
        let search = &buf[h.scan.min(available)..available];
        match find_crlf(search) {
            None | Some(LineEnd::BareCr) => {
                // If the buffered tail is a bare CR, leave the cursor
                // on it: the next feed may complete CRLF. Only do this
                // when the CR lies beyond the current line start (it
                // cannot be a stray intra-line CR in that case — stray
                // CRs are caught once a line is fully terminated).
                let tail_cr = search.last() == Some(&b'\r')
                    && h.scan + search.len() > h.line_start;
                h.scan = if tail_cr { available - 1 } else { available };
                if let Some(e) = partial_head_error(&h, available, lim) {
                    return Step::Error(Machine::Head(h), e);
                }
                return Step::Incomplete(Machine::Head(h));
            }
            Some(LineEnd::BareLf(rel)) => {
                let at = h.scan + rel;
                return Step::Error(
                    Machine::Head(h),
                    FrameError::new(ErrorKind::BadLineEnding, at),
                );
            }
            Some(LineEnd::Crlf(rel)) => {
                let cr_at = h.scan + rel;
                let line_end = cr_at + 2;
                let line = &buf[h.line_start..cr_at];

                // Stray CR inside a line is illegal regardless of what
                // follows (kills `\rContent-Length`-style smuggling).
                if let Some(p) = line.iter().position(|&b| b == b'\r') {
                    let e = FrameError::new(
                        ErrorKind::BadLineEnding,
                        h.line_start + p,
                    );
                    return Step::Error(Machine::Head(h), e);
                }

                if h.method.is_none() {
                    if line.len() > lim.max_request_line_bytes {
                        let e = FrameError::new(
                            ErrorKind::RequestLineTooLarge,
                            h.line_start,
                        );
                        return Step::Error(Machine::Head(h), e);
                    }
                    match parser::parse_request_line(line) {
                        Err(mut e) => {
                            e.offset = e.offset.saturating_add(h.line_start);
                            return Step::Error(Machine::Head(h), e);
                        }
                        Ok((method, target, _version)) => {
                            h.method = Some(method);
                            h.target = Some(target);
                        }
                    }
                } else if line.is_empty() {
                    let block_len = line_end - h.req_start;
                    if block_len > lim.max_header_block_bytes {
                        let e =
                            FrameError::new(ErrorKind::HeadersTooLarge, h.req_start);
                        return Step::Error(Machine::Head(h), e);
                    }
                    return start_body(h, line_end, lim);
                } else {
                    if h.headers.len() >= lim.max_header_count {
                        return Step::Error(
                            Machine::Head(h),
                            FrameError::new(ErrorKind::HeadersTooLarge, cr_at),
                        );
                    }
                    match parser::parse_header_line(line) {
                        Err(mut e) => {
                            e.offset = e.offset.saturating_add(h.line_start);
                            return Step::Error(Machine::Head(h), e);
                        }
                        Ok(header) => {
                            if let Err(e) = apply_header(&mut h, &header, lim) {
                                return Step::Error(Machine::Head(h), e);
                            }
                            h.headers.push(header);
                        }
                    }
                }

                h.line_start = line_end;
                h.scan = line_end;
            }
        }
    }
}

/// Once the bytes already buffered for the current line/block exceed
/// the budget, the violation is definitive: a terminator can only make
/// the line longer, never shorter.
fn partial_head_error(h: &HeadState, available: usize, lim: &Limits) -> Option<FrameError> {
    let line_so_far = available.saturating_sub(h.line_start);
    if h.method.is_none() && line_so_far > lim.max_request_line_bytes {
        return Some(FrameError::new(
            ErrorKind::RequestLineTooLarge,
            h.line_start,
        ));
    }
    if h.method.is_some() {
        let block_so_far = available.saturating_sub(h.req_start);
        if block_so_far > lim.max_header_block_bytes {
            return Some(FrameError::new(ErrorKind::HeadersTooLarge, h.req_start));
        }
    }
    None
}

enum LineEnd {
    /// Index of CR relative to the searched slice.
    Crlf(usize),
    /// Index of a bare LF relative to the searched slice.
    BareLf(usize),
    /// Final byte is CR awaiting its possible LF.
    BareCr,
}

fn find_crlf(s: &[u8]) -> Option<LineEnd> {
    for (i, &b) in s.iter().enumerate() {
        if b == b'\n' {
            if i > 0 && s[i - 1] == b'\r' {
                return Some(LineEnd::Crlf(i - 1));
            }
            return Some(LineEnd::BareLf(i));
        }
        if b == b'\r' && i + 1 == s.len() {
            return Some(LineEnd::BareCr);
        }
    }
    None
}

/// CL/TE subset rules, applied in arrival order.
fn apply_header(h: &mut HeadState, header: &Header, lim: &Limits) -> Result<(), FrameError> {
    if header.is("Transfer-Encoding") {
        if h.cl_seen {
            return Err(FrameError::new(
                ErrorKind::TeWithContentLength,
                h.line_start,
            ));
        }
        if h.te_seen {
            return Err(FrameError::new(
                ErrorKind::InvalidTransferEncoding,
                h.line_start,
            ));
        }
        if !header.value.eq_ignore_ascii_case(b"chunked") {
            return Err(FrameError::new(
                ErrorKind::InvalidTransferEncoding,
                h.line_start,
            ));
        }
        h.te_seen = true;
    } else if header.is("Content-Length") {
        if h.te_seen {
            return Err(FrameError::new(
                ErrorKind::TeWithContentLength,
                h.line_start,
            ));
        }
        if h.cl_seen {
            return Err(FrameError::new(
                ErrorKind::DuplicateContentLength,
                h.line_start,
            ));
        }
        let n = parser::parse_content_length(&header.value)?;
        if n > lim.max_body_bytes {
            return Err(FrameError::new(ErrorKind::BodyTooLarge, h.line_start));
        }
        h.cl_seen = true;
        h.cl = Some(n);
    }
    Ok(())
}

/// Head complete: install the appropriate body machine.
fn start_body(h: HeadState, body_start: usize, _lim: &Limits) -> Step {
    let HeadState {
        req_start: _,
        line_start: _,
        scan: _,
        method,
        target,
        headers,
        cl,
        cl_seen: _,
        te_seen,
    } = h;
    let method = method.expect("request line parsed");
    let target = target.expect("request line parsed");

    if te_seen {
        Step::Transition(Machine::Chunked(ChunkedState {
            region_start: body_start,
            scan: body_start,
            phase: ChunkPhase::SizeLine,
            chunk_size: 0,
            total: 0,
            body: Vec::new(),
            method,
            target,
            headers,
            trailers: Vec::new(),
        }))
    } else {
        let (framing, remaining) = match cl {
            Some(n) => (Framing::ContentLength(n), n),
            None => (Framing::NoBody, 0),
        };
        if remaining == 0 {
            Step::Frame {
                request: Request {
                    method,
                    target,
                    version: "HTTP/1.1".to_string(),
                    headers,
                    framing,
                    body: Vec::new(),
                    trailers: Vec::new(),
                },
                end: body_start,
                next: Machine::Head(HeadState::new(body_start)),
            }
        } else {
            Step::Transition(Machine::Fixed(FixedState {
                body_start,
                remaining,
                method,
                target,
                headers,
                framing,
                body: Vec::new(),
            }))
        }
    }
}

// ---------------------------------------------------------------------------
// Fixed-length body
// ---------------------------------------------------------------------------

fn pump_fixed(mut f: FixedState, buf: &[u8], available: usize) -> Step {
    let have = available.saturating_sub(f.body_start) as u64;
    let take = have.min(f.remaining);
    if take > 0 {
        let start = f.body_start;
        let end = start + take as usize;
        // Body bytes are opaque; control bytes are legal payload.
        f.body.extend_from_slice(&buf[start..end]);
        f.body_start = end;
        f.remaining -= take;
    }
    if f.remaining > 0 {
        return Step::Incomplete(Machine::Fixed(f));
    }
    let end = f.body_start;
    let FixedState {
        body_start: _,
        remaining: _,
        method,
        target,
        headers,
        framing,
        body,
    } = f;
    Step::Frame {
        request: Request {
            method,
            target,
            version: "HTTP/1.1".to_string(),
            headers,
            framing,
            body,
            trailers: Vec::new(),
        },
        end,
        next: Machine::Head(HeadState::new(end)),
    }
}

// ---------------------------------------------------------------------------
// Chunked body
// ---------------------------------------------------------------------------

fn pump_chunked(mut c: ChunkedState, buf: &[u8], available: usize, lim: &Limits) -> Step {
    loop {
        match c.phase {
            ChunkPhase::SizeLine => {
                let search = &buf[c.scan.min(available)..available];
                match find_crlf(search) {
                    None | Some(LineEnd::BareCr) => {
                        let tail_cr = search.last() == Some(&b'\r')
                            && c.scan + search.len() > c.region_start;
                        c.scan = if tail_cr { available - 1 } else { available };
                        return Step::Incomplete(Machine::Chunked(c));
                    }
                    Some(LineEnd::BareLf(rel)) => {
                        let e = FrameError::new(ErrorKind::BadLineEnding, c.scan + rel);
                        return Step::Error(Machine::Chunked(c), e);
                    }
                    Some(LineEnd::Crlf(rel)) => {
                        let cr_at = c.scan + rel;
                        let line_end = cr_at + 2;
                        let line = &buf[c.region_start..cr_at];
                        if line.contains(&b'\r') {
                            let e =
                                FrameError::new(ErrorKind::BadLineEnding, c.region_start);
                            return Step::Error(Machine::Chunked(c), e);
                        }
                        let size = match parser::parse_chunk_size(line) {
                            Ok(n) => n,
                            Err(mut e) => {
                                e.offset = e.offset.saturating_add(c.region_start);
                                return Step::Error(Machine::Chunked(c), e);
                            }
                        };
                        if size > lim.max_chunk_size {
                            let e =
                                FrameError::new(ErrorKind::ChunkTooLarge, c.region_start);
                            return Step::Error(Machine::Chunked(c), e);
                        }
                        if c.total.saturating_add(size) > lim.max_body_bytes {
                            let e = FrameError::new(ErrorKind::BodyTooLarge, c.region_start);
                            return Step::Error(Machine::Chunked(c), e);
                        }
                        c.chunk_size = size;
                        c.region_start = line_end;
                        c.scan = line_end;
                        c.phase = if size == 0 {
                            ChunkPhase::Trailer
                        } else {
                            ChunkPhase::Data
                        };
                    }
                }
            }

            ChunkPhase::Data => {
                let data_end = (c.region_start as u64)
                    .saturating_add(c.chunk_size)
                    as usize;
                if available < data_end {
                    return Step::Incomplete(Machine::Chunked(c));
                }
                c.body
                    .extend_from_slice(&buf[c.region_start..data_end]);
                c.total += c.chunk_size;
                c.region_start = data_end;
                c.scan = data_end;
                c.phase = ChunkPhase::CrlfAfterData;
            }

            ChunkPhase::CrlfAfterData => {
                // Exactly CRLF must terminate the chunk data. With both
                // bytes present anything else is a hard error; a
                // trailing lone CR waits for more input like the head.
                if available < c.region_start + 1 {
                    return Step::Incomplete(Machine::Chunked(c));
                }
                if buf[c.region_start] != b'\r' {
                    let e =
                        FrameError::new(ErrorKind::ChunkTerminator, c.region_start);
                    return Step::Error(Machine::Chunked(c), e);
                }
                if available < c.region_start + 2 {
                    return Step::Incomplete(Machine::Chunked(c));
                }
                if buf[c.region_start + 1] != b'\n' {
                    let e = FrameError::new(
                        ErrorKind::BadLineEnding,
                        c.region_start + 1,
                    );
                    return Step::Error(Machine::Chunked(c), e);
                }
                let next = c.region_start + 2;
                c.region_start = next;
                c.scan = next;
                c.phase = ChunkPhase::SizeLine;
            }

            ChunkPhase::Trailer => {
                let search = &buf[c.scan.min(available)..available];
                match find_crlf(search) {
                    None | Some(LineEnd::BareCr) => {
                        let tail_cr = search.last() == Some(&b'\r')
                            && c.scan + search.len() > c.region_start;
                        c.scan = if tail_cr { available - 1 } else { available };
                        // Trailer section shares the header-block
                        // budget measured from its own start.
                        let so_far = available.saturating_sub(c.region_start);
                        if so_far > lim.max_header_block_bytes
                            || c.trailers.len() >= lim.max_header_count
                        {
                            let e = FrameError::new(
                                ErrorKind::HeadersTooLarge,
                                c.region_start,
                            );
                            return Step::Error(Machine::Chunked(c), e);
                        }
                        return Step::Incomplete(Machine::Chunked(c));
                    }
                    Some(LineEnd::BareLf(rel)) => {
                        let e = FrameError::new(ErrorKind::BadLineEnding, c.scan + rel);
                        return Step::Error(Machine::Chunked(c), e);
                    }
                    Some(LineEnd::Crlf(rel)) => {
                        let cr_at = c.scan + rel;
                        let line_end = cr_at + 2;
                        let line = &buf[c.region_start..cr_at];
                        if line.contains(&b'\r') {
                            let e =
                                FrameError::new(ErrorKind::BadLineEnding, c.region_start);
                            return Step::Error(Machine::Chunked(c), e);
                        }
                        if line.is_empty() {
                            // End of trailer section: request complete.
                            let end = line_end;
                            let ChunkedState {
                                region_start: _,
                                scan: _,
                                phase: _,
                                chunk_size: _,
                                total: _,
                                body,
                                method,
                                target,
                                headers,
                                trailers,
                            } = c;
                            return Step::Frame {
                                request: Request {
                                    method,
                                    target,
                                    version: "HTTP/1.1".to_string(),
                                    headers,
                                    framing: Framing::Chunked,
                                    body,
                                    trailers,
                                },
                                end,
                                next: Machine::Head(HeadState::new(end)),
                            };
                        }
                        match parser::parse_header_line(line) {
                            Err(mut e) => {
                                e.offset = e.offset.saturating_add(c.region_start);
                                return Step::Error(Machine::Chunked(c), e);
                            }
                            Ok(header) => {
                                if header.is("Content-Length")
                                    || header.is("Transfer-Encoding")
                                {
                                    let e = FrameError::new(
                                        ErrorKind::TrailerForbiddenField,
                                        c.region_start,
                                    );
                                    return Step::Error(Machine::Chunked(c), e);
                                }
                                c.trailers.push(header);
                            }
                        }
                        c.region_start = line_end;
                        c.scan = line_end;
                    }
                }
            }
        }
    }
}
