//! Hand-written incremental RESP2 parser.
//!
//! No third-party protocol crate is used: the byte grammar below is
//! implemented directly in this module.
//!
//! ```text
//! value  := simple | error | integer | bulk | null | array
//! simple := "+" content  CRLF
//! error  := "-" content  CRLF
//! integer:= ":" signed   CRLF
//! bulk   := "$" length   CRLF payload(n) CRLF     ; payload is raw bytes,
//!                                                 ; CRLF may appear inside
//! null   := "$-1" CRLF | "*-1" CRLF
//! array  := "*" length   CRLF value{length}
//! ```
//!
//! Incremental semantics:
//!
//! * [`Parser::feed`] appends arbitrary chunks (one TCP read, half a frame,
//!   or a pipeline of many frames — all fine).
//! * [`Parser::try_next`] returns [`Poll::Pending`] when the buffer does not
//!   yet hold a complete frame. This is normal, not an error.
//! * A fatal protocol violation poisons the parser permanently; the caller
//!   (a server) should close the connection — RESP has no self-resync marker.

use crate::error::{ParseError, Poll};
use crate::value::Value;

/// Tunable limits. Every requirement in the task ("nested depth and total
/// byte budget are configurable", length cap, error types) lives here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Maximum declared payload length `n` of a single bulk string.
    pub max_bulk_length: usize,
    /// Maximum declared element count of a single array.
    pub max_array_length: usize,
    /// Maximum array nesting *level*. The outermost array is level 0, so with
    /// `max_depth = 2`, `[[[]]]` is legal but `[[[[]]]]` is not.
    pub max_depth: usize,
    /// Maximum number of content bytes on a `+`/`-`/`:`/`$`/`*` header line
    /// (excluding its CRLF). Caps unbounded line growth when no CRLF arrives.
    pub max_line_length: usize,
    /// Cumulative byte budget across **all complete frames consumed by one
    /// parser**. Bytes of a frame that is still incomplete do not count yet.
    pub max_total_bytes: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            max_bulk_length: 512 * 1024 * 1024,
            max_array_length: 1_000_000,
            max_depth: 7,
            max_line_length: 64 * 1024,
            max_total_bytes: 64 * 1024 * 1024,
        }
    }
}

impl Config {
    /// Config useful for tests: tight, deterministic limits.
    pub fn tight() -> Self {
        Config {
            max_bulk_length: 16,
            max_array_length: 8,
            max_depth: 2,
            max_line_length: 32,
            max_total_bytes: 128,
        }
    }
}

/// Internal decode outcome: incomplete is a *retryable* condition, the
/// `ParseError` arm is fatal.
enum Decode {
    Incomplete,
    Fatal(ParseError),
}

type DecodeResult<T> = Result<T, Decode>;

/// Streaming, incremental parser.
pub struct Parser {
    config: Config,
    buf: Vec<u8>,
    /// Bytes belonging to complete frames already removed from `buf`.
    consumed_bytes: usize,
    poisoned: bool,
}

impl Parser {
    pub fn new(config: Config) -> Self {
        Parser {
            config,
            buf: Vec::new(),
            consumed_bytes: 0,
            poisoned: false,
        }
    }

    /// Bytes currently buffered and not yet consumed as a complete frame.
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }

    /// Cumulative bytes of all complete frames consumed so far.
    pub fn consumed_bytes(&self) -> usize {
        self.consumed_bytes
    }

    /// Total bytes this parser may consume (mirror of [`Config::max_total_bytes`]).
    pub fn total_budget(&self) -> usize {
        self.config.max_total_bytes
    }

    /// Append a chunk of network bytes. Any chunking is legal, including
    /// splitting in the middle of a CRLF or inside a bulk payload.
    pub fn feed(&mut self, chunk: &[u8]) -> Result<(), ParseError> {
        if self.poisoned {
            return Err(ParseError::ParserPoisoned);
        }
        self.buf.extend_from_slice(chunk);
        Ok(())
    }

    /// Attempt to parse one frame from the front of the buffer.
    ///
    /// * [`Poll::Ready`] removes the frame's bytes from the buffer and counts
    ///   them against the total byte budget.
    /// * [`Poll::Pending`] asks for more bytes (the buffer is untouched).
    /// * [`Poll::Error`] poisons the parser; the connection should be closed.
    pub fn try_next(&mut self) -> Poll<Value> {
        if self.poisoned {
            return Poll::Error(ParseError::ParserPoisoned);
        }
        if self.buf.is_empty() {
            return Poll::Pending;
        }
        let mut remaining = self.config.max_total_bytes - self.consumed_bytes;
        match decode_value(&self.buf, 0, &mut remaining, 0, &self.config) {
            Ok((value, end)) => {
                // `remaining` was decremented by exactly `end` bytes.
                self.consumed_bytes += end;
                self.buf.drain(..end);
                Poll::Ready(value)
            }
            Err(Decode::Incomplete) => Poll::Pending,
            Err(Decode::Fatal(e)) => {
                self.poisoned = true;
                Poll::Error(e)
            }
        }
    }

    /// Whether the parser has seen a fatal error and can no longer be used.
    pub fn is_poisoned(&self) -> bool {
        self.poisoned
    }
}

/// Stateless one-shot decode: parse exactly one frame starting at `input[0]`.
///
/// Returns the value and the index just past the frame on success,
/// `None` when `input` holds fewer than a complete frame (feed more bytes),
/// or a fatal [`ParseError`].
pub fn try_parse(input: &[u8], config: &Config) -> Result<Option<(Value, usize)>, ParseError> {
    let mut remaining = config.max_total_bytes;
    match decode_value(input, 0, &mut remaining, 0, config) {
        Ok((v, end)) => Ok(Some((v, end))),
        Err(Decode::Incomplete) => Ok(None),
        Err(Decode::Fatal(e)) => Err(e),
    }
}

/// Parse one value at `pos`.
///
/// Every byte the frame consumes (headers, CRLFs, bulk payloads) is charged
/// against `*remaining_budget`. Because the function is only called when a
/// *complete* frame can be produced (it returns `Incomplete` otherwise), the
/// budget is never charged for partial data.
fn decode_value(
    buf: &[u8],
    pos: usize,
    remaining_budget: &mut usize,
    depth: usize,
    config: &Config,
) -> DecodeResult<(Value, usize)> {
    let type_byte = match buf.get(pos) {
        Some(&b) => b,
        None => return Err(Decode::Incomplete),
    };
    let (line, after_line) = read_line(buf, pos, config)?;
    charge(after_line - pos, remaining_budget)?;

    match type_byte {
        b'+' => {
            let s = parse_utf8(line, "simple string")?;
            Ok((Value::Simple(s), after_line))
        }
        b'-' => {
            let s = parse_utf8(line, "error")?;
            Ok((Value::Error(s), after_line))
        }
        b':' => {
            let n = parse_signed(line, "integer")?;
            Ok((Value::Integer(n), after_line))
        }
        b'$' => decode_bulk(buf, after_line, line, remaining_budget, config),
        b'*' => decode_array(buf, after_line, line, remaining_budget, depth, config),
        other => Err(Decode::Fatal(ParseError::UnknownTypeByte(other))),
    }
}

/// Charge `n` consumed bytes against the cumulative budget.
fn charge(n: usize, remaining: &mut usize) -> DecodeResult<()> {
    if n > *remaining {
        return Err(Decode::Fatal(ParseError::BudgetExceeded {
            needed: n,
            remaining: *remaining,
        }));
    }
    *remaining -= n;
    Ok(())
}

/// Locate the CRLF-terminated header line starting at `pos`.
///
/// Returns `(content_without_type_byte_or_crlf, index_after_crlf)`.
fn read_line<'a>(buf: &'a [u8], pos: usize, config: &Config) -> DecodeResult<(&'a [u8], usize)> {
    let content_start = pos + 1; // skip the type byte
    let mut i = content_start;
    loop {
        let b = match buf.get(i) {
            None => {
                if i - content_start > config.max_line_length {
                    return Err(Decode::Fatal(ParseError::LineTooLong {
                        length: i - content_start,
                        max: config.max_line_length,
                    }));
                }
                return Err(Decode::Incomplete);
            }
            Some(&b) => b,
        };
        if b == b'\r' {
            // A CR must be followed immediately by LF.
            match buf.get(i + 1) {
                None => return Err(Decode::Incomplete), // CR may be half of a CRLF
                Some(&b'\n') => {
                    let content_len = i - content_start;
                    if content_len > config.max_line_length {
                        return Err(Decode::Fatal(ParseError::LineTooLong {
                            length: content_len,
                            max: config.max_line_length,
                        }));
                    }
                    return Ok((&buf[content_start..i], i + 2));
                }
                Some(&other) => return Err(Decode::Fatal(ParseError::InvalidLineEnding { byte: other })),
            }
        }
        if b == b'\n' {
            return Err(Decode::Fatal(ParseError::InvalidLineEnding { byte: b'\n' }));
        }
        i += 1;
    }
}

fn parse_utf8(raw: &[u8], field: &'static str) -> DecodeResult<String> {
    match std::str::from_utf8(raw) {
        Ok(s) => Ok(s.to_owned()),
        Err(_) => Err(Decode::Fatal(ParseError::InvalidUtf8 {
            field,
            raw: raw.to_vec(),
        })),
    }
}

/// Parse `[ - ] digit+` (an optional leading minus, then ASCII digits).
/// Leading zeroes are accepted; a bare `-`, a `+`, embedded whitespace and
/// any other byte are rejected.
fn parse_signed(raw: &[u8], field: &'static str) -> DecodeResult<i64> {
    let invalid = || {
        Decode::Fatal(ParseError::InvalidInteger {
            field,
            raw: raw.to_vec(),
        })
    };
    let (neg, digits) = match raw.first() {
        Some(&b'-') => (true, &raw[1..]),
        Some(&(b'0'..=b'9')) => (false, raw),
        _ => return Err(invalid()),
    };
    if digits.is_empty() {
        return Err(invalid());
    }
    // Accumulate in u64 so that the magnitude of i64::MIN (which exceeds
    // i64::MAX by 1) can still be parsed for a negative field.
    let mut magnitude: u64 = 0;
    for &d in digits {
        if !d.is_ascii_digit() {
            return Err(invalid());
        }
        magnitude = match magnitude
            .checked_mul(10)
            .and_then(|v| v.checked_add((d - b'0') as u64))
        {
            Some(v) => v,
            None => return Err(Decode::Fatal(ParseError::IntegerOverflow(raw.to_vec()))),
        };
    }
    let upper = if neg {
        (i64::MAX as u64) + 1 // 9_223_372_036_854_775_808
    } else {
        i64::MAX as u64
    };
    if magnitude > upper {
        return Err(Decode::Fatal(ParseError::IntegerOverflow(raw.to_vec())));
    }
    if neg {
        // magnitude <= |i64::MIN|; the +1 case wraps exactly to i64::MIN.
        // `as` casts do not panic on out-of-range values; wrapping_neg maps
        // i64::MIN to itself, which is the desired result.
        Ok((magnitude as i64).wrapping_neg())
    } else {
        Ok(magnitude as i64)
    }
}

fn decode_bulk(
    buf: &[u8],
    after_line: usize,
    line: &[u8],
    remaining_budget: &mut usize,
    config: &Config,
) -> DecodeResult<(Value, usize)> {
    // Strict grammar: the only legal signed bulk length is the null marker
    // `-1`. `-0`, `-2`, `-007` are all protocol violations — a length is a
    // non-negative integer.
    if line.first() == Some(&b'-') {
        if line == b"-1" {
            return Ok((Value::Null, after_line));
        }
        let n = parse_signed(line, "bulk length")?; // reports malformed forms
        return Err(Decode::Fatal(ParseError::InvalidBulkLength(n)));
    }
    let n = parse_signed(line, "bulk length")?;
    debug_assert!(n >= 0);
    let n = n as usize;

    if n > config.max_bulk_length {
        return Err(Decode::Fatal(ParseError::BulkTooLarge {
            declared: n,
            max: config.max_bulk_length,
        }));
    }

    // Reject early, even when only the header has arrived: a declared frame
    // larger than the whole remaining budget can never succeed.
    let tail = n + 2; // payload plus its CRLF trailer
    if tail > *remaining_budget {
        return Err(Decode::Fatal(ParseError::BudgetExceeded {
            needed: tail,
            remaining: *remaining_budget,
        }));
    }

    let payload_end = after_line + n;
    // The trailer must exist and be exactly CRLF. The payload in between is
    // unconstrained: embedded CRLF / arbitrary binary is normal bulk data.
    match (buf.get(payload_end), buf.get(payload_end + 1)) {
        (None, _) | (Some(_), None) => return Err(Decode::Incomplete),
        (Some(&b'\r'), Some(&b'\n')) => {}
        (Some(&other), Some(_)) => {
            return Err(Decode::Fatal(ParseError::InvalidLineEnding { byte: other }))
        }
    }
    charge(tail, remaining_budget)?;
    Ok((
        Value::Bulk(buf[after_line..payload_end].to_vec()),
        payload_end + 2,
    ))
}

fn decode_array(
    buf: &[u8],
    after_line: usize,
    line: &[u8],
    remaining_budget: &mut usize,
    depth: usize,
    config: &Config,
) -> DecodeResult<(Value, usize)> {
    // Strict grammar: as for bulk lengths, the only signed value allowed is
    // the null marker `-1`; `-0` and friends are violations.
    if line.first() == Some(&b'-') {
        if line == b"-1" {
            return Ok((Value::Null, after_line));
        }
        return Err(Decode::Fatal(ParseError::InvalidInteger {
            field: "array length",
            raw: line.to_vec(),
        }));
    }
    let n = parse_signed(line, "array length")?;
    debug_assert!(n >= 0);
    let count = n as usize;
    if count > config.max_array_length {
        return Err(Decode::Fatal(ParseError::ArrayTooLarge {
            declared: count,
            max: config.max_array_length,
        }));
    }
    // The outermost array is decoded at depth 0; each nested array adds 1.
    if depth > config.max_depth {
        return Err(Decode::Fatal(ParseError::NestingTooDeep {
            depth,
            max: config.max_depth,
        }));
    }

    let mut items = Vec::with_capacity(count.min(16));
    let mut cursor = after_line;
    for _ in 0..count {
        let (value, next) = decode_value(buf, cursor, remaining_budget, depth + 1, config)?;
        items.push(value);
        cursor = next;
    }
    Ok((Value::Array(items), cursor))
}
