//! Shared test utilities:
//! * a byte-split differential runner that proves framing is independent of
//!   TCP segmentation (split at EVERY byte position);
//! * a tiny raw HTTP/1 response reader for server end-to-end tests.

#![allow(dead_code)]

use http_framing::framer::Framer;
use http_framing::{ErrorKind, ParseError, Step};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

/// Compact, comparable description of what happened to a buffer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Trace {
    /// `(method, target, framing tag, body length, trailer count, consumed)` per frame
    Frames(Vec<(String, String, &'static str, usize, usize, usize)>),
    /// Frames before the error, then the error classification.
    Error(
        Vec<(String, String, &'static str, usize, usize, usize)>,
        ErrorKind,
    ),
}

fn frame_trace(
    f: &http_framing::RequestFrame,
) -> (String, String, &'static str, usize, usize, usize) {
    let tag = match f.framing {
        http_framing::Framing::None => "none",
        http_framing::Framing::FixedLen(_) => "fixed-length",
        http_framing::Framing::Chunked => "chunked",
    };
    (
        String::from_utf8_lossy(&f.method).into_owned(),
        String::from_utf8_lossy(&f.target).into_owned(),
        tag,
        f.body.len(),
        f.trailers.len(),
        f.consumed,
    )
}

/// Reference result: feed the whole buffer in one call.
pub fn reference_trace(input: &[u8]) -> Trace {
    let mut framer = Framer::new();
    let mut pos = 0;
    let mut frames = Vec::new();
    loop {
        match framer.step(&input[pos..]) {
            (Step::Frame(f), used) => {
                pos += used;
                frames.push(frame_trace(&f));
            }
            (Step::Incomplete, used) => {
                pos += used;
                assert_eq!(pos, input.len(), "Incomplete must absorb the tail");
                return Trace::Frames(frames);
            }
            (Step::Error(err), used) => {
                let _ = used;
                return Trace::Error(frames, err.kind);
            }
        }
    }
}

/// Reference with custom limits.
pub fn reference_trace_with(input: &[u8], limits: http_framing::Limits) -> Trace {
    let mut framer = Framer::with_limits(limits);
    let mut pos = 0;
    let mut frames = Vec::new();
    loop {
        match framer.step(&input[pos..]) {
            (Step::Frame(f), used) => {
                pos += used;
                frames.push(frame_trace(&f));
            }
            (Step::Incomplete, used) => {
                pos += used;
                assert_eq!(pos, input.len());
                return Trace::Frames(frames);
            }
            (Step::Error(err), _) => return Trace::Error(frames, err.kind),
        }
    }
}

/// Drive a fresh framer with the given segment boundaries.
pub fn trace_with_cuts(input: &[u8], cuts: &[usize]) -> Trace {
    let mut framer = Framer::new();
    let mut frames = Vec::new();
    let mut start = 0usize;
    for &cut in cuts {
        let cut = cut.clamp(start, input.len());
        if cut < start {
            continue;
        }
        let seg = &input[start..cut];
        start = cut;
        match framer.step(seg) {
            (Step::Frame(f), used) => {
                // A frame may be returned while the segment still contains
                // bytes for the next pipelined request: keep stepping.
                frames.push(frame_trace(&f));
                let mut rest = &seg[used..];
                loop {
                    match framer.step(rest) {
                        (Step::Frame(f2), u2) => {
                            frames.push(frame_trace(&f2));
                            rest = &rest[u2..];
                        }
                        (Step::Incomplete, _) => break,
                        (Step::Error(e), _) => return Trace::Error(frames, e.kind),
                    }
                }
            }
            (Step::Incomplete, _) => {}
            (Step::Error(e), _) => return Trace::Error(frames, e.kind),
        }
    }
    if start < input.len() {
        match framer.step(&input[start..]) {
            (Step::Frame(f), _) => {
                frames.push(frame_trace(&f));
            }
            (Step::Incomplete, _) => {}
            (Step::Error(e), _) => return Trace::Error(frames, e.kind),
        }
    }
    Trace::Frames(frames)
}

/// Drive a fresh framer with the given segment boundaries under custom limits.
pub fn trace_with_cuts_limited(
    input: &[u8],
    cuts: &[usize],
    limits: http_framing::Limits,
) -> Trace {
    let mut framer = Framer::with_limits(limits);
    let mut frames = Vec::new();
    let mut start = 0usize;
    for &cut in cuts {
        let cut = cut.clamp(start, input.len());
        if cut < start {
            continue;
        }
        let seg = &input[start..cut];
        start = cut;
        let mut rest = seg;
        loop {
            match framer.step(rest) {
                (Step::Frame(f), used) => {
                    frames.push(frame_trace(&f));
                    rest = &rest[used..];
                }
                (Step::Incomplete, _) => break,
                (Step::Error(e), _) => return Trace::Error(frames, e.kind),
            }
        }
    }
    if start < input.len() {
        let mut rest = &input[start..];
        loop {
            match framer.step(rest) {
                (Step::Frame(f), used) => {
                    frames.push(frame_trace(&f));
                    rest = &rest[used..];
                }
                (Step::Incomplete, _) => break,
                (Step::Error(e), _) => return Trace::Error(frames, e.kind),
            }
        }
    }
    Trace::Frames(frames)
}

/// Deterministic pseudo-random cut schedules (xorshift).
pub fn random_cuts(len: usize, schedules: usize, seed: u64) -> Vec<Vec<usize>> {
    let mut state = seed.max(1);
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    let mut out = Vec::new();
    for _ in 0..schedules {
        let mut cuts = Vec::new();
        let mut pos = 0usize;
        while pos < len {
            let gap = (next() % 7).max(1) as usize;
            pos = (pos + gap).min(len);
            cuts.push(pos);
        }
        if cuts.last() != Some(&len) {
            cuts.push(len);
        }
        out.push(cuts);
    }
    out
}

/// Assert that every single split point produces the reference trace.
pub fn assert_every_single_split_matches(input: &[u8]) {
    let expected = reference_trace(input);
    for cut in 0..=input.len() {
        let got = trace_with_cuts(input, &[cut, input.len()]);
        assert_eq!(got, expected, "single split mismatch at byte {cut}");
    }
}

/// Assert every single split under custom limits.
pub fn assert_every_single_split_matches_limited(input: &[u8], limits: http_framing::Limits) {
    let expected = reference_trace_with(input, limits);
    for cut in 0..=input.len() {
        let got = trace_with_cuts_limited(input, &[cut, input.len()], limits);
        assert_eq!(got, expected, "single split mismatch at byte {cut}");
    }
}

// ------------------------------------------------------------------
// raw HTTP/1 response reader for server tests
// ------------------------------------------------------------------

pub struct RawResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

fn find_double_crlf(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|p| p + 4)
}

/// Buffered connection that lets tests send raw bytes and read framed
/// responses back, including pipelined ones.
pub struct Conn {
    pub stream: TcpStream,
    pub leftover: Vec<u8>,
}

impl Conn {
    pub fn connect(addr: &str) -> std::io::Result<Self> {
        let stream = TcpStream::connect(addr)?;
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        Ok(Conn {
            stream,
            leftover: Vec::new(),
        })
    }

    pub fn send(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        self.stream.write_all(bytes)
    }

    pub fn send_str(&mut self, s: &str) -> std::io::Result<()> {
        self.send(s.as_bytes())
    }

    /// Read exactly one response (requires Content-Length framing).
    pub fn read_one(&mut self) -> std::io::Result<RawResponse> {
        let mut tmp = [0u8; 4096];
        let end = loop {
            if let Some(e) = find_double_crlf(&self.leftover) {
                break e;
            }
            let n = self.stream.read(&mut tmp)?;
            if n == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "EOF before response headers",
                ));
            }
            self.leftover.extend_from_slice(&tmp[..n]);
        };

        let head = self.leftover[..end - 4].to_vec();
        let head_text = String::from_utf8_lossy(&head);
        let mut lines = head_text.split("\r\n");
        let status_line = lines.next().unwrap_or("");
        let status = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        let mut headers = Vec::new();
        let mut content_length = None;
        for line in lines {
            if let Some((k, v)) = line.split_once(':') {
                let k = k.trim().to_string();
                let v = v.trim().to_string();
                if k.eq_ignore_ascii_case("content-length") {
                    content_length = v.parse().ok();
                }
                headers.push((k, v));
            }
        }

        let len = content_length.unwrap_or(0);
        while self.leftover.len() < end + len {
            let n = self.stream.read(&mut tmp)?;
            if n == 0 {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "EOF mid response body",
                ));
            }
            self.leftover.extend_from_slice(&tmp[..n]);
        }
        let body = self.leftover[end..end + len].to_vec();
        self.leftover.drain(..end + len);
        Ok(RawResponse {
            status,
            headers,
            body,
        })
    }

    /// Expect the server to close the connection (error responses).
    pub fn expect_closed(&mut self) -> std::io::Result<Vec<u8>> {
        let mut tmp = [0u8; 4096];
        let mut data = std::mem::take(&mut self.leftover);
        loop {
            match self.stream.read(&mut tmp) {
                Ok(0) => return Ok(data),
                Ok(n) => data.extend_from_slice(&tmp[..n]),
                Err(err)
                    if matches!(
                        err.kind(),
                        std::io::ErrorKind::UnexpectedEof
                            | std::io::ErrorKind::ConnectionReset
                            | std::io::ErrorKind::BrokenPipe
                    ) =>
                {
                    return Ok(data)
                }
                Err(err)
                    if err.kind() == std::io::ErrorKind::WouldBlock
                        || err.kind() == std::io::ErrorKind::TimedOut =>
                {
                    return Ok(data)
                }
                Err(err) => return Err(err),
            }
        }
    }
}

/// Convenience: parse the JSON body minimally to pull a field via string match.
pub fn json_has(body: &[u8], needle: &str) -> bool {
    String::from_utf8_lossy(body).contains(needle)
}

#[allow(dead_code)]
pub fn err_of(e: ParseError) -> ErrorKind {
    e.kind
}
