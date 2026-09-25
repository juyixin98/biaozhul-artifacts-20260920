use crate::value::{Config, Value};
use std::fmt;

/// Errors reported by the parser.
///
/// `Incomplete` is used internally to signal "need more bytes"; it is
/// never returned from [`Parser::next`] — that returns `Ok(None)` instead.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// More bytes are needed to finish the current message (internal).
    Incomplete,
    /// The leading byte is not one of `+ - : $ *`.
    InvalidTypeByte(u8),
    /// A numeric field (integer reply, bulk length, array length) is not
    /// a valid base-10 integer.
    InvalidNumber,
    /// A bulk/array length is negative but not the RESP null marker `-1`.
    NegativeLength(i64),
    /// A line or bulk payload is not terminated by CRLF as required.
    MissingCrlf,
    /// A simple string or error line is not valid UTF-8.
    InvalidUtf8,
    /// Nesting of arrays exceeded `Config::max_depth`.
    DepthLimitExceeded,
    /// The unparsed buffer grew past `Config::max_buffer_bytes`.
    BufferBudgetExceeded,
    /// A bulk string length exceeded `Config::max_bulk_len`.
    BulkLengthLimitExceeded,
    /// An array element count exceeded `Config::max_array_len`.
    ArrayLengthLimitExceeded,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseError::Incomplete => write!(f, "incomplete message, more bytes needed"),
            ParseError::InvalidTypeByte(b) => {
                write!(f, "invalid type byte 0x{:02x} ({:?})", b, *b as char)
            }
            ParseError::InvalidNumber => write!(f, "invalid number"),
            ParseError::NegativeLength(n) => write!(f, "invalid negative length {}", n),
            ParseError::MissingCrlf => write!(f, "missing CRLF terminator"),
            ParseError::InvalidUtf8 => write!(f, "invalid UTF-8 in line"),
            ParseError::DepthLimitExceeded => write!(f, "nesting depth limit exceeded"),
            ParseError::BufferBudgetExceeded => write!(f, "buffer byte budget exceeded"),
            ParseError::BulkLengthLimitExceeded => write!(f, "bulk string length limit exceeded"),
            ParseError::ArrayLengthLimitExceeded => write!(f, "array length limit exceeded"),
        }
    }
}

impl std::error::Error for ParseError {}

/// Incremental RESP2 parser. Feed bytes, then pull complete messages.
///
/// After [`Parser::next`] returns a hard `Err`, the stream position is
/// undefined (the offending bytes may still be in the buffer); call
/// [`Parser::reset`] before reusing the parser for a fresh stream.
pub struct Parser {
    buf: Vec<u8>,
    cfg: Config,
}

impl Parser {
    pub fn new(cfg: Config) -> Self {
        Parser {
            buf: Vec::new(),
            cfg,
        }
    }

    /// Append bytes to the internal buffer.
    ///
    /// Fails with [`ParseError::BufferBudgetExceeded`] if the buffer would
    /// grow past `Config::max_buffer_bytes` (the bytes are not appended).
    pub fn feed(&mut self, data: &[u8]) -> Result<(), ParseError> {
        if self.buf.len() + data.len() > self.cfg.max_buffer_bytes {
            return Err(ParseError::BufferBudgetExceeded);
        }
        self.buf.extend_from_slice(data);
        Ok(())
    }

    /// Try to parse one complete message from the buffer.
    ///
    /// - `Ok(Some(value))`: a message was parsed and removed from the buffer.
    /// - `Ok(None)`: not enough bytes yet; feed more.
    /// - `Err(e)`: the bytes at the head of the buffer are malformed or
    ///   violate a configured limit.
    pub fn next(&mut self) -> Result<Option<Value>, ParseError> {
        match parse_value(&self.buf, 0, &self.cfg, 1) {
            Ok((value, end)) => {
                self.buf.drain(..end);
                Ok(Some(value))
            }
            Err(ParseError::Incomplete) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Discard all buffered bytes (use after a hard parse error).
    pub fn reset(&mut self) {
        self.buf.clear();
    }

    /// Number of unparsed bytes currently buffered.
    pub fn buffered_len(&self) -> usize {
        self.buf.len()
    }
}

impl Default for Parser {
    fn default() -> Self {
        Parser::new(Config::default())
    }
}

/// Find the next CRLF in `buf` at or after `from`; returns the index of `\r`.
fn find_crlf(buf: &[u8], from: usize) -> Option<usize> {
    if from >= buf.len() {
        return None;
    }
    buf[from..]
        .windows(2)
        .position(|w| w == b"\r\n")
        .map(|i| from + i)
}

/// Parse a line terminated by CRLF starting at `pos`.
/// Returns `(line_bytes, next_pos)` where `next_pos` is just past the CRLF.
fn parse_line<'a>(buf: &'a [u8], pos: usize) -> Result<(&'a [u8], usize), ParseError> {
    let end = find_crlf(buf, pos).ok_or(ParseError::Incomplete)?;
    Ok((&buf[pos..end], end + 2))
}

/// Parse a base-10 i64 (optional `+`/`-` sign, at least one digit).
fn parse_i64(bytes: &[u8]) -> Result<i64, ParseError> {
    let s = std::str::from_utf8(bytes).map_err(|_| ParseError::InvalidNumber)?;
    if s.is_empty() {
        return Err(ParseError::InvalidNumber);
    }
    let (sign, digits) = match s.as_bytes()[0] {
        b'-' => (-1i64, &s[1..]),
        b'+' => (1i64, &s[1..]),
        _ => (1i64, s),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ParseError::InvalidNumber);
    }
    // Accumulate in i128 so i64::MIN ("-9223372036854775808") parses fine.
    let mut n: i128 = 0;
    for b in digits.bytes() {
        n = n * 10 + (b - b'0') as i128;
        if n > (i64::MAX as i128) + 1 {
            return Err(ParseError::InvalidNumber);
        }
    }
    let n = sign as i128 * n;
    i64::try_from(n).map_err(|_| ParseError::InvalidNumber)
}

/// Parse one value starting at `pos`. `depth` is 1 for a top-level value.
/// Returns `(value, next_pos)`.
fn parse_value(
    buf: &[u8],
    pos: usize,
    cfg: &Config,
    depth: usize,
) -> Result<(Value, usize), ParseError> {
    if depth > cfg.max_depth {
        return Err(ParseError::DepthLimitExceeded);
    }
    if pos >= buf.len() {
        return Err(ParseError::Incomplete);
    }
    match buf[pos] {
        b'+' => {
            let (line, next) = parse_line(buf, pos + 1)?;
            let s = std::str::from_utf8(line).map_err(|_| ParseError::InvalidUtf8)?;
            Ok((Value::SimpleString(s.to_string()), next))
        }
        b'-' => {
            let (line, next) = parse_line(buf, pos + 1)?;
            let s = std::str::from_utf8(line).map_err(|_| ParseError::InvalidUtf8)?;
            Ok((Value::Error(s.to_string()), next))
        }
        b':' => {
            let (line, next) = parse_line(buf, pos + 1)?;
            Ok((Value::Integer(parse_i64(line)?), next))
        }
        b'$' => {
            let (line, mut next) = parse_line(buf, pos + 1)?;
            let len = parse_i64(line)?;
            if len == -1 {
                return Ok((Value::BulkString(None), next));
            }
            if len < 0 {
                return Err(ParseError::NegativeLength(len));
            }
            let len = len as usize;
            if len > cfg.max_bulk_len {
                return Err(ParseError::BulkLengthLimitExceeded);
            }
            // Need `len` payload bytes plus the trailing CRLF.
            let need = len.checked_add(2).ok_or(ParseError::BulkLengthLimitExceeded)?;
            if buf.len() - next < need {
                return Err(ParseError::Incomplete);
            }
            let payload = buf[next..next + len].to_vec();
            next += len;
            if &buf[next..next + 2] != b"\r\n" {
                return Err(ParseError::MissingCrlf);
            }
            Ok((Value::BulkString(Some(payload)), next + 2))
        }
        b'*' => {
            let (line, mut next) = parse_line(buf, pos + 1)?;
            let count = parse_i64(line)?;
            if count == -1 {
                return Ok((Value::Array(None), next));
            }
            if count < 0 {
                return Err(ParseError::NegativeLength(count));
            }
            let count = count as usize;
            if count > cfg.max_array_len {
                return Err(ParseError::ArrayLengthLimitExceeded);
            }
            let mut items = Vec::with_capacity(count.min(1024));
            for _ in 0..count {
                let (item, n) = parse_value(buf, next, cfg, depth + 1)?;
                items.push(item);
                next = n;
            }
            Ok((Value::Array(Some(items)), next))
        }
        other => Err(ParseError::InvalidTypeByte(other)),
    }
}
