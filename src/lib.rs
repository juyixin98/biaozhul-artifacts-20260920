//! HTTP 分帧一致性 — incremental HTTP/1.1 request framing library.
//!
//! The crate is deliberately dependency free and uses no unsafe code. Start
//! with [`framer::Framer`] for the incremental parser; [`server`] wraps it in
//! a local TCP test service. [`parse_all`] is a convenience wrapper used by
//! the tests.

#![forbid(unsafe_code)]

pub mod error;
pub mod framer;
pub mod server;
pub mod text;

pub use error::{ErrorKind, ParseError};
pub use framer::{Framer, Framing, Header, Limits, RequestFrame, Step};

/// Outcome of feeding a whole buffer through a fresh framer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseAll {
    /// Every complete frame delimited, in order.
    pub frames: Vec<RequestFrame>,
    /// Bytes belonging to a frame that was started but not completed
    /// (a trailing partial request).
    pub incomplete_bytes: usize,
    /// First hard error, if the stream violates the subset.
    pub error: Option<ParseError>,
}

/// Parse a complete buffer with a fresh default framer (pipelining aware).
///
/// When `error` is set, `incomplete_bytes` reports how many leading bytes
/// were consumed before (and including) the error detection point — this is
/// the same value a series of incremental [`Framer::step`] calls would
/// consume, which the byte-split tests assert.
pub fn parse_all(input: &[u8]) -> ParseAll {
    parse_all_with(input, Limits::default())
}

/// Same as [`parse_all`] but with custom [`Limits`].
pub fn parse_all_with(input: &[u8], limits: Limits) -> ParseAll {
    let mut framer = Framer::with_limits(limits);
    let mut frames = Vec::new();
    let mut pos = 0usize;

    loop {
        match framer.step(&input[pos..]) {
            (Step::Incomplete, used) => {
                pos += used;
                return ParseAll {
                    frames,
                    incomplete_bytes: input.len() - pos,
                    error: None,
                };
            }
            (Step::Frame(frame), used) => {
                pos += used;
                frames.push(frame);
            }
            (Step::Error(err), used) => {
                pos += used;
                return ParseAll {
                    frames,
                    incomplete_bytes: input.len() - pos,
                    error: Some(err),
                };
            }
        }
    }
}
