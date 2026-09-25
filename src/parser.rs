//! Pure parsing primitives for the supported HTTP/1.1 subset.
//!
//! These functions operate on a single already-delimited line (without
//! its trailing `CRLF`) and do no buffering themselves; the streaming
//! state machine lives in [`crate::framer`]. Keeping the grammar checks
//! here makes them easy to unit-test and makes it impossible for the
//! incremental path to apply different rules than the one-shot path.
//!
//! [`Header::is`] does case-insensitive field-name comparison.

use crate::error::{ErrorKind, FrameError};

/// One header/trailer field. Names preserve the sender's casing;
/// comparisons are performed case-insensitively via [`Header::is`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub name: String,
    pub value: Vec<u8>,
}

impl Header {
    pub fn new(name: &str, value: &[u8]) -> Self {
        Header {
            name: name.to_string(),
            value: value.to_vec(),
        }
    }

    /// Case-insensitive name comparison, ASCII only.
    pub fn is(&self, name: &str) -> bool {
        self.name.len() == name.len()
            && self
                .name
                .as_bytes()
                .iter()
                .zip(name.as_bytes().iter())
                .all(|(a, b)| a.eq_ignore_ascii_case(b))
    }
}

/// How a request's body is framed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Framing {
    /// No body was declared (no `Content-Length`, no `Transfer-Encoding`).
    NoBody,
    /// `Content-Length: n` with the (validated) declared length.
    ContentLength(u64),
    /// `Transfer-Encoding: chunked`.
    Chunked,
}

/// A fully framed request. The body is the *decoded* body: raw bytes for
/// content-length, chunk-data concatenated for chunked (chunk metadata,
/// size lines and trailers are removed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub method: String,
    pub target: String,
    pub version: String,
    pub headers: Vec<Header>,
    pub framing: Framing,
    pub body: Vec<u8>,
    /// Trailer fields (empty unless chunked trailers were sent).
    pub trailers: Vec<Header>,
}

impl Request {
    /// First value for a header, case-insensitively.
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.headers.iter().find(|h| h.is(name)).map(|h| h.value.as_slice())
    }
}

/// RFC 7230 §3.2.6 token character.
pub fn is_tchar(b: u8) -> bool {
    matches!(
        b,
        b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*'
        | b'+' | b'-' | b'.' | b'^' | b'_' | b'`' | b'|' | b'~'
        | b'0'..=b'9' | b'A'..=b'Z' | b'a'..=b'z'
    )
}

fn err(kind: ErrorKind, at: usize) -> FrameError {
    FrameError::new(kind, at)
}

/// Parse a request line (without CRLF): `SP method SP request-target SP
/// HTTP-version`.
///
/// Exactly two single-SP separators; tabs and additional spaces are
/// rejected as ambiguous whitespace.
pub fn parse_request_line(line: &[u8]) -> Result<(String, String, String), FrameError> {
    let first_sp = memchr(line, b' ');
    let second_sp = first_sp
        .and_then(|i| line[i + 1..].iter().position(|&b| b == b' ').map(|j| i + 1 + j));

    let (i, j) = match (first_sp, second_sp) {
        (Some(i), Some(j)) => (i, j),
        _ => return Err(err(ErrorKind::MalformedRequestLine, line.len().saturating_sub(1))),
    };

    // Anything else separating the fields is a grammar violation:
    // tabs, trailing whitespace or a third token.
    if line[j + 1..].iter().any(|&b| b == b' ' || b == b'\t') {
        return Err(err(ErrorKind::AmbiguousWhitespace, j + 1));
    }
    if i == 0 || j == i + 1 || j + 1 == line.len() {
        return Err(err(ErrorKind::MalformedRequestLine, 0));
    }

    let method = &line[..i];
    let target = &line[i + 1..j];
    let version = &line[j + 1..];

    if method.iter().any(|&b| !is_tchar(b)) {
        return Err(err(ErrorKind::InvalidMethod, 0));
    }

    // Request target: origin-form (starts with '/'), or "*" for OPTIONS.
    let target_ok = (target.starts_with(b"/")
        && target
            .iter()
            .all(|&b| (0x21..=0x7E).contains(&b) && b != b' '))
        || (method == b"OPTIONS" && target == b"*");
    if !target_ok {
        return Err(err(ErrorKind::InvalidTarget, i + 1));
    }

    if version != b"HTTP/1.1" {
        // HTTP/1.0 and friends are outside the supported subset; also
        // catches garbage versions as a single stable error.
        if version.starts_with(b"HTTP/") {
            return Err(err(ErrorKind::UnsupportedVersion, j + 1));
        }
        return Err(err(ErrorKind::MalformedRequestLine, j + 1));
    }

    // Safety: every byte above was verified ASCII/VCHAR.
    Ok((
        String::from_utf8(method.to_vec()).unwrap(),
        String::from_utf8(target.to_vec()).unwrap(),
        String::from_utf8(version.to_vec()).unwrap(),
    ))
}

fn memchr(haystack: &[u8], needle: u8) -> Option<usize> {
    haystack.iter().position(|&b| b == needle)
}

/// Parse one header field line (without CRLF) into name/value.
///
/// Grammar enforced (deliberately stricter than RFC 7230's tolerant
/// receiver rules, because leniency is exactly what enables smuggling):
///
/// * obs-fold is rejected by the caller (this function must never see
///   a line beginning with SP/HT), and re-checked defensively.
/// * name is 1+ `tchar` bytes — no whitespace before the colon.
/// * exactly one `:`; nothing after the name but the colon.
/// * at most one optional leading SP/HT is consumed; any further
///   leading/trailing OWS is ambiguous whitespace.
/// * value bytes are VCHAR or HT only.
pub fn parse_header_line(line: &[u8]) -> Result<Header, FrameError> {
    if line.is_empty() {
        return Err(err(ErrorKind::HeaderMissingColon, 0));
    }
    // Lines arrive here split on bare/CRLF boundaries; an initial
    // whitespace line means obs-fold.
    if line[0] == b' ' || line[0] == b'\t' {
        return Err(err(ErrorKind::ObsoleteLineFolding, 0));
    }

    let colon = memchr(line, b':').ok_or_else(|| {
        err(ErrorKind::HeaderMissingColon, line.len().saturating_sub(1))
    })?;
    let name = &line[..colon];
    let mut value = &line[colon + 1..];

    if name.is_empty() || name.iter().any(|&b| !is_tchar(b)) {
        // Covers "Name : value" (space before colon) as well as control
        // characters — the classic `X: y\r\nContent-Length: 0` versus
        // `Content Length: 0` desync.
        return Err(err(ErrorKind::InvalidHeaderName, 0));
    }

    // Consume *at most one* leading OWS byte of each kind, then reject
    // remaining leading whitespace as ambiguous. RFC allows OWS; we
    // deliberately accept exactly one SP (the common form) and treat
    // HT/double-space as ambiguous so senders cannot hide a value.
    if let Some(rest) = value.strip_prefix(b" ") {
        value = rest;
    }
    if value.first() == Some(&b' ') || value.first() == Some(&b'\t') {
        return Err(err(ErrorKind::AmbiguousWhitespace, colon + 1));
    }

    if value.last() == Some(&b' ') || value.last() == Some(&b'\t') {
        return Err(err(ErrorKind::AmbiguousWhitespace, line.len() - 1));
    }

    // Field content is VCHAR with single internal SP allowed between
    // visible characters (e.g. "5, 5" must reach the Content-Length
    // grammar and be rejected there, not here). Tabs are refused as a
    // visual-desync vector; empty values are fine.
    let mut prev_space = false;
    for (idx, &b) in value.iter().enumerate() {
        match b {
            b'\t' | 0..=0x1F | 0x7F.. => {
                return Err(err(ErrorKind::InvalidHeaderValue, colon + 1 + idx));
            }
            b' ' if prev_space => {
                return Err(err(ErrorKind::AmbiguousWhitespace, colon + 1 + idx));
            }
            b' ' => prev_space = true,
            _ => prev_space = false,
        }
    }

    Ok(Header::new(
        std::str::from_utf8(name).expect("tchar is ASCII"),
        value,
    ))
}

/// Parse a strict decimal content-length value.
///
/// No whitespace, no sign, no `012` leading zero, no empty value, no
/// overflow. `Content-Length: 12, 12` (comma list) never reaches here:
/// the comma is rejected as a non-digit.
pub fn parse_content_length(value: &[u8]) -> Result<u64, FrameError> {
    if value.is_empty() {
        return Err(err(ErrorKind::InvalidContentLength, 0));
    }
    if !value.iter().all(|b| b.is_ascii_digit()) {
        return Err(err(ErrorKind::InvalidContentLength, 0));
    }
    if value.len() > 1 && value[0] == b'0' {
        return Err(err(ErrorKind::InvalidContentLength, 0));
    }
    let mut n: u64 = 0;
    for &b in value {
        n = n
            .checked_mul(10)
            .and_then(|v| v.checked_add((b - b'0') as u64))
            .ok_or_else(|| err(ErrorKind::InvalidContentLength, 0))?;
    }
    Ok(n)
}

/// Parse a chunk-size line.
///
/// Allows 1+ ASCII hex digits. Chunk extensions are *not* supported by
/// the subset; any `;`, whitespace or other suffix is rejected.
pub fn parse_chunk_size(line: &[u8]) -> Result<u64, FrameError> {
    if line.is_empty() {
        return Err(err(ErrorKind::ChunkSizeInvalid, 0));
    }
    let mut n: u64 = 0;
    let mut digits = 0usize;
    for (idx, &b) in line.iter().enumerate() {
        let d = match b {
            b'0'..=b'9' => (b - b'0') as u64,
            b'a'..=b'f' => (b - b'a' + 10) as u64,
            b'A'..=b'F' => (b - b'A' + 10) as u64,
            b';' => return Err(err(ErrorKind::ChunkSizeInvalid, idx)),
            b' ' | b'\t' => return Err(err(ErrorKind::AmbiguousWhitespace, idx)),
            _ => return Err(err(ErrorKind::ChunkSizeInvalid, idx)),
        };
        n = n
            .checked_mul(16)
            .and_then(|v| v.checked_add(d))
            .ok_or_else(|| err(ErrorKind::ChunkSizeInvalid, idx))?;
        digits += 1;
    }
    // No leading-zero restriction: `00000000;` would already be
    // rejected by the extension rule, and RFC allows leading zeros
    // (though discourages them).
    let _ = digits;
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_line_ok() {
        let (m, t, v) = parse_request_line(b"GET / HTTP/1.1").unwrap();
        assert_eq!((m.as_str(), t.as_str(), v.as_str()), ("GET", "/", "HTTP/1.1"));
        let (m, t, _) = parse_request_line(b"OPTIONS * HTTP/1.1").unwrap();
        assert_eq!((m.as_str(), t.as_str()), ("OPTIONS", "*"));
    }

    #[test]
    fn request_line_bad() {
        assert_eq!(
            parse_request_line(b"GET /").unwrap_err().kind,
            ErrorKind::MalformedRequestLine
        );
        assert_eq!(
            parse_request_line(b"GET  / HTTP/1.1").unwrap_err().kind,
            ErrorKind::AmbiguousWhitespace
        );
        assert_eq!(
            parse_request_line(b"GET\t/ HTTP/1.1").unwrap_err().kind,
            ErrorKind::MalformedRequestLine
        );
        assert_eq!(
            parse_request_line(b"GET / HTTP/1.0").unwrap_err().kind,
            ErrorKind::UnsupportedVersion
        );
        assert_eq!(
            parse_request_line(b"GE T / HTTP/1.1").unwrap_err().kind,
            ErrorKind::AmbiguousWhitespace
        );
        assert_eq!(
            parse_request_line(b"GE\tT / HTTP/1.1").unwrap_err().kind,
            ErrorKind::InvalidMethod
        );
        assert_eq!(
            parse_request_line(b"GET badtarget HTTP/1.1").unwrap_err().kind,
            ErrorKind::InvalidTarget
        );
        assert_eq!(
            parse_request_line(b"GET / HTTP/1.1 junk").unwrap_err().kind,
            ErrorKind::AmbiguousWhitespace
        );
    }

    #[test]
    fn header_ok_and_bad() {
        let h = parse_header_line(b"Host: example.com").unwrap();
        assert_eq!(h.name, "Host");
        assert_eq!(h.value, b"example.com");
        let h = parse_header_line(b"X-Empty:").unwrap();
        assert!(h.value.is_empty());

        for (line, kind) in [
            (&b"Bad Header: x"[..], ErrorKind::InvalidHeaderName),
            (&b"Bad\tHeader: x"[..], ErrorKind::InvalidHeaderName),
            (&b"X : x"[..], ErrorKind::InvalidHeaderName),
            (&b"X:  x"[..], ErrorKind::AmbiguousWhitespace),
            (&b"X:\tx"[..], ErrorKind::AmbiguousWhitespace),
            (&b"X: x " [..], ErrorKind::AmbiguousWhitespace),
            (&b"NoColon"[..], ErrorKind::HeaderMissingColon),
            (&b"X: a\x01b"[..], ErrorKind::InvalidHeaderValue),
            (&b" folded"[..], ErrorKind::ObsoleteLineFolding),
        ] {
            assert_eq!(
                parse_header_line(line).unwrap_err().kind,
                kind,
                "line={line:?}"
            );
        }
    }

    #[test]
    fn content_length_rules() {
        assert_eq!(parse_content_length(b"0").unwrap(), 0);
        assert_eq!(parse_content_length(b"123").unwrap(), 123);
        for bad in [&b""[..], &b" 12"[..], &b"12 "[..], &b"012"[..],
                    &b"-1"[..], &b"12, 12"[..], &b"0x10"[..]] {
            assert_eq!(
                parse_content_length(bad).unwrap_err().kind,
                ErrorKind::InvalidContentLength,
                "value={bad:?}"
            );
        }
        assert_eq!(
            parse_content_length(b"99999999999999999999999999").unwrap_err().kind,
            ErrorKind::InvalidContentLength
        );
    }

    #[test]
    fn chunk_size_rules() {
        assert_eq!(parse_chunk_size(b"4").unwrap(), 4);
        assert_eq!(parse_chunk_size(b"1aF").unwrap(), 0x1af);
        for bad in [&b""[..], &b" 4"[..], &b"4 "[..], &b"4;ext=1"[..],
                    &b"xyz"[..], &b"-1"[..]] {
            let expected = if bad.starts_with(b" ") || bad.ends_with(b" ") {
                ErrorKind::AmbiguousWhitespace
            } else {
                ErrorKind::ChunkSizeInvalid
            };
            assert_eq!(
                parse_chunk_size(bad).unwrap_err().kind,
                expected,
                "line={bad:?}"
            );
        }
        assert_eq!(
            parse_chunk_size(b"fffffffffffffffff").unwrap_err().kind,
            ErrorKind::ChunkSizeInvalid
        );
    }
}
