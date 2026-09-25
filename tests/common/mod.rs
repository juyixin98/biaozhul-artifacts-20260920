//! Shared test corpus and drivers.
//!
//! Per Cargo convention `tests/common/mod.rs` is compiled as a module,
//! not a standalone test binary.

use std::borrow::Cow;

use http_framing::{ErrorKind, Framer, Limits, Request};

/// Expected whole-stream outcome of a corpus entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Valid,
    Invalid(ErrorKind),
}

#[derive(Debug, Clone)]
pub struct Case {
    pub name: &'static str,
    pub input: Cow<'static, [u8]>,
    pub outcome: Outcome,
    pub limits: Limits,
}

fn mk(name: &'static str, input: &'static [u8], outcome: Outcome) -> Case {
    Case {
        name,
        input: Cow::Borrowed(input),
        outcome,
        limits: Limits::default(),
    }
}

fn mk_with_limits(
    name: &'static str,
    input: Vec<u8>,
    outcome: Outcome,
    limits: Limits,
) -> Case {
    Case {
        name,
        input: Cow::Owned(input),
        outcome,
        limits,
    }
}

pub fn cases() -> Vec<Case> {
    let mut v = Vec::new();
    valid_cases(&mut v);
    invalid_cases(&mut v);
    limit_cases(&mut v);
    v
}

fn valid_cases(out: &mut Vec<Case>) {
    out.push(mk(
        "get-minimal",
        b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "get-no-headers",
        b"HEAD /foo?bar=1 HTTP/1.1\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "options-star",
        b"OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "fixed-length",
        b"POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello",
        Outcome::Valid,
    ));
    out.push(mk(
        "content-length-zero",
        b"POST /z HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "fixed-body-with-crlf-inside",
        b"POST /b HTTP/1.1\r\nHost: x\r\nContent-Length: 9\r\n\r\nx\r\nGET /y",
        Outcome::Valid,
    ));
    out.push(mk(
        "chunked-basic-no-trailer",
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          3\r\nfoo\r\n0\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "chunked-multi-with-trailer",
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          4\r\nWiki\r\n5\r\npedia\r\n0\r\nETag: \"abc\"\r\nX-Trailer: 1\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "chunked-data-looking-like-crlf",
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          4\r\na\r\nb\r\n0\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "chunked-uppercase-coding-and-hex",
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: ChUnKeD\r\n\r\n\
          1A\r\nabcdefghijklmnopqrstuvwxyz\r\n0\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "pipeline-three-fixed",
        b"GET /1 HTTP/1.1\r\nHost: x\r\n\r\n\
          POST /2 HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc\
          GET /3 HTTP/1.1\r\nHost: x\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "pipeline-chunked-then-get",
        b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          3\r\nfoo\r\n0\r\n\r\n\
          GET /next HTTP/1.1\r\nHost: y\r\n\r\n",
        Outcome::Valid,
    ));
    out.push(mk(
        "pipeline-fixed-then-chunked",
        b"POST /a HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\nhi\
          POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
          1\r\nz\r\n0\r\n\r\n",
        Outcome::Valid,
    ));
}

fn invalid_cases(out: &mut Vec<Case>) {
    // --- request smuggling ambiguity classes --------------------------
    out.push(mk(
        "smuggle-cl-then-te",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n\
          Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n\
          GET /admin HTTP/1.1\r\nHost: x\r\n\r\n",
        Outcome::Invalid(ErrorKind::TeWithContentLength),
    ));
    out.push(mk(
        "smuggle-te-then-cl",
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\
          Content-Length: 4\r\n\r\n0\r\n\r\n\
          GET /admin HTTP/1.1\r\nHost: x\r\n\r\n",
        Outcome::Invalid(ErrorKind::TeWithContentLength),
    ));
    out.push(mk(
        "smuggle-duplicate-cl-equal",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\
          Content-Length: 5\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::DuplicateContentLength),
    ));
    out.push(mk(
        "smuggle-duplicate-cl-conflict",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n\
          Content-Length: 5\r\n\r\nhelloX",
        Outcome::Invalid(ErrorKind::DuplicateContentLength),
    ));
    out.push(mk(
        "smuggle-cl-comma-list",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5, 5\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::InvalidContentLength),
    ));
    out.push(mk(
        "smuggle-cl-leading-zero",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 05\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::InvalidContentLength),
    ));
    out.push(mk(
        "smuggle-space-before-colon",
        b"POST / HTTP/1.1\r\nContent-Length : 5\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::InvalidHeaderName),
    ));
    out.push(mk(
        "smuggle-space-in-name",
        b"POST / HTTP/1.1\r\nContent Length: 5\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::InvalidHeaderName),
    ));
    out.push(mk(
        "smuggle-obs-fold-space",
        b"POST / HTTP/1.1\r\nHost: x\r\nX: a\r\n Content-Length: 5\r\n\r\n",
        Outcome::Invalid(ErrorKind::ObsoleteLineFolding),
    ));
    out.push(mk(
        "smuggle-obs-fold-tab",
        b"POST / HTTP/1.1\r\nHost: x\r\nX: a\r\n\tContent-Length: 5\r\n\r\n",
        Outcome::Invalid(ErrorKind::ObsoleteLineFolding),
    ));
    out.push(mk(
        "smuggle-bare-lf",
        b"POST / HTTP/1.1\nHost: x\nContent-Length: 5\n\nhello",
        Outcome::Invalid(ErrorKind::BadLineEnding),
    ));
    out.push(mk(
        "smuggle-stray-cr-in-line",
        b"GET / HTTP/1.1\rX: y\r\n\r\n",
        Outcome::Invalid(ErrorKind::BadLineEnding),
    ));

    // --- TE subset ------------------------------------------------------
    out.push(mk(
        "te-identity",
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: identity\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidTransferEncoding),
    ));
    out.push(mk(
        "te-gzip-chunked-list",
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip, chunked\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidTransferEncoding),
    ));
    out.push(mk(
        "te-duplicate",
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\
          Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidTransferEncoding),
    ));

    // --- whitespace -----------------------------------------------------
    out.push(mk(
        "ws-double-space-after-colon",
        b"GET / HTTP/1.1\r\nHost:  example.com\r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "ws-tab-after-colon",
        b"GET / HTTP/1.1\r\nHost:\texample.com\r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "ws-trailing-in-value",
        b"GET / HTTP/1.1\r\nHost: example.com \r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "ws-tab-in-request-line",
        b"GET\t/ HTTP/1.1\r\n\r\n",
        Outcome::Invalid(ErrorKind::MalformedRequestLine),
    ));
    out.push(mk(
        "ws-double-space-request-line",
        b"GET  / HTTP/1.1\r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "ws-trailing-request-line",
        b"GET / HTTP/1.1 \r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));

    // --- request line grammar ------------------------------------------
    out.push(mk(
        "rl-two-tokens",
        b"GET /\r\n\r\n",
        Outcome::Invalid(ErrorKind::MalformedRequestLine),
    ));
    out.push(mk(
        "rl-four-tokens",
        b"GET / HTTP/1.1 junk\r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "rl-http-1-0",
        b"GET / HTTP/1.0\r\n\r\n",
        Outcome::Invalid(ErrorKind::UnsupportedVersion),
    ));
    out.push(mk(
        "rl-absolute-form",
        b"GET http://evil.example/ HTTP/1.1\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidTarget),
    ));
    out.push(mk(
        "rl-bad-method",
        b"GE\tT / HTTP/1.1\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidMethod),
    ));
    out.push(mk(
        "header-no-colon",
        b"GET / HTTP/1.1\r\nBadHeader\r\n\r\n",
        Outcome::Invalid(ErrorKind::HeaderMissingColon),
    ));
    out.push(mk(
        "header-ctl-in-value",
        b"GET / HTTP/1.1\r\nX: a\x01b\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidHeaderValue),
    ));

    // --- content-length grammar ----------------------------------------
    out.push(mk(
        "cl-plus-sign",
        b"POST / HTTP/1.1\r\nContent-Length: +5\r\n\r\nhello",
        Outcome::Invalid(ErrorKind::InvalidContentLength),
    ));
    out.push(mk(
        "cl-hex",
        b"POST / HTTP/1.1\r\nContent-Length: 0x10\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidContentLength),
    ));
    out.push(mk(
        "cl-overflow",
        b"POST / HTTP/1.1\r\nContent-Length: 99999999999999999999\r\n\r\n",
        Outcome::Invalid(ErrorKind::InvalidContentLength),
    ));

    // --- chunked grammar ------------------------------------------------
    out.push(mk(
        "chunk-extension",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
          4;name=value\r\nWiki\r\n0\r\n\r\n",
        Outcome::Invalid(ErrorKind::ChunkSizeInvalid),
    ));
    out.push(mk(
        "chunk-size-non-hex",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        Outcome::Invalid(ErrorKind::ChunkSizeInvalid),
    ));
    out.push(mk(
        "chunk-size-space",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n4 \r\nWiki\r\n0\r\n\r\n",
        Outcome::Invalid(ErrorKind::AmbiguousWhitespace),
    ));
    out.push(mk(
        "chunk-terminator-garbage",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nfooX\r\n0\r\n\r\n",
        Outcome::Invalid(ErrorKind::ChunkTerminator),
    ));
    out.push(mk(
        "chunk-terminator-bare-lf",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nfoo\n0\r\n\r\n",
        Outcome::Invalid(ErrorKind::ChunkTerminator),
    ));
    out.push(mk(
        "chunk-trailer-forbidden-cl",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
          0\r\nContent-Length: 5\r\n\r\n",
        Outcome::Invalid(ErrorKind::TrailerForbiddenField),
    ));
    out.push(mk(
        "chunk-trailer-forbidden-te",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
          0\r\nTransfer-Encoding: chunked\r\n\r\n",
        Outcome::Invalid(ErrorKind::TrailerForbiddenField),
    ));
    out.push(mk(
        "chunk-trailer-fold",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: a\r\n b\r\n\r\n",
        Outcome::Invalid(ErrorKind::ObsoleteLineFolding),
    ));

    // --- truncation (only surfaces at end_input) -----------------------
    out.push(mk(
        "truncated-head",
        b"GET / HTTP/1.1\r\nHost: example.com\r\n",
        Outcome::Invalid(ErrorKind::Incomplete),
    ));
    out.push(mk(
        "truncated-fixed-body",
        b"POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhel",
        Outcome::Invalid(ErrorKind::Incomplete),
    ));
    out.push(mk(
        "truncated-chunked",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc",
        Outcome::Invalid(ErrorKind::Incomplete),
    ));
}

/// Oversize/over-count cases, each paired with tight custom limits.
fn limit_cases(out: &mut Vec<Case>) {
    let small = Limits {
        max_request_line_bytes: 32,
        max_header_block_bytes: 64,
        max_header_count: 4,
        max_body_bytes: 15,
        max_chunk_size: 8,
    };

    // Request line exceeds the cap (terminated properly; rejection
    // must already fire while the line streams in).
    let mut long_line = b"GET /".to_vec();
    long_line.extend(std::iter::repeat_n(b'a', 64));
    long_line.extend_from_slice(b" HTTP/1.1\r\n\r\n");
    out.push(mk_with_limits(
        "limit-request-line",
        long_line,
        Outcome::Invalid(ErrorKind::RequestLineTooLarge),
        small.clone(),
    ));

    // Header block exceeds the byte cap.
    let big_block = b"GET / HTTP/1.1\r\n\
                     X-A: 000000000000000000000000000000000000000000000000000000000\r\n\
                     X-B: 000000000000000000000000000000000000000000000000000000000\r\n\r\n"
        .to_vec();
    out.push(mk_with_limits(
        "limit-header-block",
        big_block,
        Outcome::Invalid(ErrorKind::HeadersTooLarge),
        small.clone(),
    ));

    // Header count cap.
    let many = b"GET / HTTP/1.1\r\n\
                A: 1\r\nB: 2\r\nC: 3\r\nD: 4\r\nE: 5\r\n\r\n"
        .to_vec();
    out.push(mk_with_limits(
        "limit-header-count",
        many,
        Outcome::Invalid(ErrorKind::HeadersTooLarge),
        small.clone(),
    ));

    // Declared fixed body over the cap — rejected at the CL header.
    out.push(mk_with_limits(
        "limit-fixed-body-declared",
        b"POST / HTTP/1.1\r\nContent-Length: 100\r\n\r\n".to_vec(),
        Outcome::Invalid(ErrorKind::BodyTooLarge),
        small.clone(),
    ));

    // Chunk larger than the per-chunk cap — rejected at its size line.
    out.push(mk_with_limits(
        "limit-chunk-size",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n".to_vec(),
        Outcome::Invalid(ErrorKind::ChunkTooLarge),
        small.clone(),
    ));

    // Decoded chunked total over the body cap (8 + 8 > 16).
    out.push(mk_with_limits(
        "limit-chunked-total",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
          8\r\naaaaaaaa\r\n8\r\nbbbbbbbb\r\n0\r\n\r\n"
            .to_vec(),
        Outcome::Invalid(ErrorKind::BodyTooLarge),
        small,
    ));
}

/// Feed `input` according to a chunk-size schedule, accumulating
/// completed requests; call `end_input` after the schedule covers the
/// whole input (mapping a partial remainder to [`ErrorKind::Incomplete`]).
pub fn drive_collect(
    input: &[u8],
    schedule: &[usize],
    limits: Limits,
) -> Result<Vec<Request>, ErrorKind> {
    let mut framer = Framer::with_limits(limits);
    let mut collected = Vec::new();
    let mut pos = 0;
    for &len in schedule {
        let end = (pos + len).min(input.len());
        match framer.feed(&input[pos..end]) {
            Ok(reqs) => collected.extend(reqs),
            Err(e) => return Err(e.kind),
        }
        pos = end;
        if pos == input.len() {
            break;
        }
    }
    assert_eq!(pos, input.len(), "schedule must cover the whole input");
    match framer.end_input() {
        Ok(()) => Ok(collected),
        Err(e) => Err(e.kind),
    }
}

/// Whole input in one feed.
pub fn whole_schedule(n: usize) -> Vec<usize> {
    vec![n]
}

/// One byte per feed.
pub fn byte_schedule(n: usize) -> Vec<usize> {
    vec![1; n]
}

/// Two feeds split at byte position `k` (`0..=n`). Zero-length pieces
/// are dropped (they exercise nothing).
pub fn split_schedule(n: usize, k: usize) -> Vec<usize> {
    let mut v = vec![k, n.saturating_sub(k)];
    v.retain(|&x| x > 0);
    v
}

/// Irregular multi-piece pattern beyond single cuts.
pub fn irregular_schedule(n: usize) -> Vec<usize> {
    let pat = [1usize, 1, 2, 1, 1, 3, 1, 4, 2, 1];
    let mut v = Vec::new();
    let mut total = 0;
    let mut i = 0;
    while total < n {
        let next = pat[i % pat.len()];
        v.push(next);
        total += next;
        i += 1;
    }
    v
}
