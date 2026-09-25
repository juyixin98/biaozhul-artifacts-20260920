//! Unit tests for the framer itself:
//!   valid  -> frame shape; invalid -> exact ErrorKind; limits -> rejections.

use http_framing::framer::{Framer, Framing, Limits};
use http_framing::{ErrorKind, Step};

fn expect_single_frame(input: &[u8]) -> http_framing::RequestFrame {
    let mut f = Framer::new();
    match f.step(input) {
        (Step::Frame(frame), used) => {
            assert_eq!(used, input.len(), "frame must consume the whole input");
            frame
        }
        other => panic!("expected one frame, got {other:?}"),
    }
}

fn expect_error(input: &[u8]) -> ErrorKind {
    let mut f = Framer::new();
    match f.step(input) {
        (Step::Error(e), _) => e.kind,
        other => panic!("expected error, got {other:?}"),
    }
}

fn expect_error_limited(input: &[u8], limits: Limits) -> ErrorKind {
    let mut f = Framer::with_limits(limits);
    loop {
        let rest_start = f.position();
        let (step, _used) = f.step(&input[rest_start..]);
        match step {
            Step::Error(e) => return e.kind,
            Step::Frame(_) => continue,
            Step::Incomplete => panic!("unexpected Incomplete for reject case"),
        }
    }
}

// ==================================================================
// valid framing
// ==================================================================

#[test]
fn simple_get_no_body() {
    let f = expect_single_frame(b"GET / HTTP/1.1\r\nHost: example\r\n\r\n");
    assert_eq!(f.method, b"GET");
    assert_eq!(f.target, b"/");
    assert_eq!(f.framing, Framing::None);
    assert_eq!(f.body.len(), 0);
    assert!(f.keep_alive);
    assert_eq!(f.headers.len(), 1);
    assert_eq!(f.headers[0].name, b"Host");
    assert_eq!(f.headers[0].value, b"example");
}

#[test]
fn get_without_headers() {
    let f = expect_single_frame(b"GET / HTTP/1.1\r\n\r\n");
    assert_eq!(f.framing, Framing::None);
    assert_eq!(f.consumed, 18);
}

#[test]
fn fixed_length_body() {
    let input = b"POST /upload HTTP/1.1\r\nHost: e\r\nContent-Length: 5\r\n\r\nhello";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::FixedLen(5));
    assert_eq!(f.body, b"hello");
}

#[test]
fn fixed_length_zero_body() {
    let input = b"POST /x HTTP/1.1\r\nContent-Length: 0\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::FixedLen(0));
    assert!(f.body.is_empty());
}

#[test]
fn chunked_simple() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::Chunked);
    assert_eq!(f.body, b"hello");
    assert!(f.trailers.is_empty());
}

#[test]
fn chunked_multi_chunks() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        4\r\nWiki\r\n5\r\npedia\r\nE\r\n in\r\n\r\nchunks.\r\n0\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.body, b"Wikipedia in\r\n\r\nchunks.");
}

#[test]
fn chunked_with_trailer() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        5\r\nhello\r\n0\r\nX-Test: yes\r\nX-Other:  2 \r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.body, b"hello");
    assert_eq!(f.trailers.len(), 2);
    assert_eq!(f.trailers[0].name, b"X-Test");
    assert_eq!(f.trailers[0].value, b"yes");
    assert_eq!(f.trailers[1].value, b"2");
}

#[test]
fn chunked_with_extension() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        5; name=\"a b\"\r\nhello\r\n0; foo\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.body, b"hello");
}

#[test]
fn chunked_uppercase_encoding_name() {
    let input = b"POST /c HTTP/1.1\r\nTRANSFER-ENCODING: CHUNKED\r\n\r\n0\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::Chunked);
}

#[test]
fn pipelined_two_requests() {
    let input = b"GET /a HTTP/1.1\r\nHost: e\r\n\r\nGET /b HTTP/1.1\r\nHost: e\r\n\r\n";
    let mut f = Framer::new();
    let (s1, u1) = f.step(input);
    let f1 = match s1 {
        Step::Frame(x) => x,
        other => panic!("{other:?}"),
    };
    assert_eq!(f1.target, b"/a");
    let (s2, u2) = f.step(&input[u1..]);
    let f2 = match s2 {
        Step::Frame(x) => x,
        other => panic!("{other:?}"),
    };
    assert_eq!(f2.target, b"/b");
    assert_eq!(u1 + u2, input.len());
}

#[test]
fn pipelined_get_then_post() {
    let input = b"GET /a HTTP/1.1\r\n\r\nPOST /b HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc";
    let mut f = Framer::new();
    let (s1, u1) = f.step(input);
    assert!(matches!(s1, Step::Frame(_)));
    let (s2, u2) = f.step(&input[u1..]);
    assert!(matches!(s2, Step::Frame(_)));
    assert_eq!(u1 + u2, input.len());
}

#[test]
fn connection_close_recorded() {
    let f = expect_single_frame(b"GET / HTTP/1.1\r\nHost: e\r\nConnection: close\r\n\r\n");
    assert!(!f.keep_alive);
}

#[test]
fn header_value_with_internal_spaces_and_obs_text() {
    // spaces inside values are legal; only leading/trailing OWS is trimmed.
    let f = expect_single_frame("GET / HTTP/1.1\r\nX: a  b  c\r\n\r\n".as_bytes());
    assert_eq!(f.headers[0].value, b"a  b  c");
    // obs-text bytes are preserved as raw bytes.
    let raw = b"GET / HTTP/1.1\r\nX: \xff\xfe\r\n\r\n";
    let f = expect_single_frame(raw);
    assert_eq!(f.headers[0].value, &[0xff, 0xfe]);
}

// ==================================================================
// request line rejection
// ==================================================================

#[test]
fn rejects_bare_lf_request_line() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\nHost: x\r\n\r\n"),
        ErrorKind::BareLineFeed
    );
}

#[test]
fn rejects_bare_cr_inside_request_line() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\rHost\r\n\r\n"),
        ErrorKind::BareCarriageReturn
    );
}

#[test]
fn rejects_empty_line_before_request() {
    assert_eq!(
        expect_error(b"\r\nGET / HTTP/1.1\r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
}

#[test]
fn rejects_leading_space_request_line() {
    // Leading SP yields an empty method token under the strict 3-token shape.
    assert_eq!(
        expect_error(b" GET / HTTP/1.1\r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
}

#[test]
fn rejects_tab_in_request_line() {
    assert_eq!(
        expect_error(b"GET\t/ HTTP/1.1\r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
}

#[test]
fn rejects_extra_whitespace_request_line() {
    // double SP between tokens -> empty target
    assert_eq!(
        expect_error(b"GET  / HTTP/1.1\r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
    // trailing whitespace before CRLF
    assert_eq!(
        expect_error(b"GET / HTTP/1.1 \r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
}

#[test]
fn rejects_http_10_and_garbage_versions() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.0\r\n\r\n"),
        ErrorKind::UnsupportedVersion
    );
    assert_eq!(
        expect_error(b"GET / HTTP/2.0\r\n\r\n"),
        ErrorKind::UnsupportedVersion
    );
}

#[test]
fn rejects_bad_method_bytes() {
    assert_eq!(
        expect_error(b"GE T / HTTP/1.1\r\n\r\n"),
        ErrorKind::MalformedRequestLine
    );
}

// ==================================================================
// header rejection / ambiguous whitespace
// ==================================================================

#[test]
fn rejects_obs_fold() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nX: a\r\n b\r\n\r\n"),
        ErrorKind::ObsFold
    );
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nX: a\r\n\tb\r\n\r\n"),
        ErrorKind::ObsFold
    );
}

#[test]
fn rejects_space_before_colon() {
    // RFC 9112 explicitly flags "X : y" as smuggling ambiguity.
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nX : y\r\n\r\n"),
        ErrorKind::InvalidHeaderName
    );
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nX\t: y\r\n\r\n"),
        ErrorKind::InvalidHeaderName
    );
}

#[test]
fn rejects_header_without_colon() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nNotAHeader\r\n\r\n"),
        ErrorKind::MalformedHeader
    );
}

#[test]
fn rejects_bare_lf_inside_headers() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nHost: a\nX: b\r\n\r\n"),
        ErrorKind::BareLineFeed
    );
}

#[test]
fn rejects_control_byte_in_header_value() {
    assert_eq!(
        expect_error(b"GET / HTTP/1.1\r\nX: a\x00b\r\n\r\n"),
        ErrorKind::InvalidHeaderValue
    );
}

#[test]
fn accepts_empty_header_value() {
    let f = expect_single_frame(b"GET / HTTP/1.1\r\nX:\r\n\r\n");
    assert_eq!(f.headers[0].value, b"");
}

// ==================================================================
// length conflicts / smuggling corpus
// ==================================================================

#[test]
fn rejects_te_and_cl() {
    let input = b"POST / HTTP/1.1\r\n\
        Content-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n";
    assert_eq!(expect_error(input), ErrorKind::TeAndCl);
}

#[test]
fn rejects_te_and_cl_reversed_order() {
    let input = b"POST / HTTP/1.1\r\n\
        Transfer-Encoding: chunked\r\nContent-Length: 6\r\n\r\n0\r\n\r\n";
    assert_eq!(expect_error(input), ErrorKind::TeAndCl);
}

#[test]
fn rejects_duplicate_content_length_same_value() {
    let input = b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello";
    assert_eq!(expect_error(input), ErrorKind::DuplicateContentLength);
}

#[test]
fn rejects_duplicate_content_length_different_value() {
    let input = b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello";
    assert_eq!(expect_error(input), ErrorKind::DuplicateContentLength);
}

#[test]
fn rejects_conflicting_comma_cl() {
    let input = b"POST / HTTP/1.1\r\nContent-Length: 5, 6\r\n\r\nhello";
    assert_eq!(expect_error(input), ErrorKind::ConflictingContentLength);
}

#[test]
fn accepts_agreed_comma_cl() {
    // RFC 9112: identical comma-list values may be normalised; we accept.
    let input = b"POST / HTTP/1.1\r\nContent-Length: 5, 5\r\n\r\nhello";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::FixedLen(5));
    assert_eq!(f.body, b"hello");
}

#[test]
fn rejects_non_numeric_cl() {
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: abc\r\n\r\n"),
        ErrorKind::InvalidContentLength
    );
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: 0x10\r\n\r\n"),
        ErrorKind::InvalidContentLength
    );
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: \r\n\r\n"),
        ErrorKind::InvalidContentLength
    );
}

#[test]
fn rejects_cl_overflow() {
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: 99999999999999999999999\r\n\r\n"),
        ErrorKind::InvalidContentLength
    );
}

#[test]
fn rejects_chunked_with_other_codings() {
    // Only the single coding "chunked" is in the supported subset.
    for v in [
        "chunked, identity",
        "identity",
        "chunk",
        "gzip, chunked",
        "x",
    ] {
        let input = format!("POST / HTTP/1.1\r\nTransfer-Encoding: {v}\r\n\r\n0\r\n\r\n");
        assert_eq!(
            expect_error(input.as_bytes()),
            ErrorKind::InvalidTransferEncoding,
            "TE value `{v}` must be rejected"
        );
    }
}

#[test]
fn accepts_ows_around_chunked_token() {
    // OWS (SP/HTAB) right after ':' / before value end is RFC 7230 legal and
    // gets trimmed — an explicit, spec-based decision (unlike leading
    // whitespace on the request line or before ':').
    for v in [" chunked", "chunked", "chunked ", "\tchunked\t"] {
        let input = format!("POST / HTTP/1.1\r\nTransfer-Encoding:{v}\r\n\r\n0\r\n\r\n");
        let f = expect_single_frame(input.as_bytes());
        assert_eq!(f.framing, Framing::Chunked, "TE value `{v}`");
    }
}

#[test]
fn rejects_duplicate_te() {
    let input = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\
        Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n";
    assert_eq!(expect_error(input), ErrorKind::InvalidTransferEncoding);
}

#[test]
fn accepts_tab_ows_after_colon_in_te() {
    // Contrast with ambiguity elsewhere: HTAB after ':' is ordinary OWS.
    let input = b"POST / HTTP/1.1\r\nTransfer-Encoding:\tchunked\r\n\r\n0\r\n\r\n";
    let f = expect_single_frame(input);
    assert_eq!(f.framing, Framing::Chunked);
}

#[test]
fn rejects_cl_smuggling_via_header_name() {
    // Space/obs-fold tricks to forge a second CL.
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length : 5\r\n\r\nhello"),
        ErrorKind::InvalidHeaderName
    );
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nContent-Length: 5\r\n Content-Length: 6\r\n\r\nhello"),
        ErrorKind::ObsFold
    );
}

// ==================================================================
// chunked protocol rejection
// ==================================================================

#[test]
fn rejects_bad_chunk_size() {
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nxx\r\n"),
        ErrorKind::MalformedChunkSize
    );
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n;x\r\n"),
        ErrorKind::MalformedChunkSize
    );
}

#[test]
fn rejects_chunk_size_overflow() {
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nfffffffffffffffff\r\n"),
        ErrorKind::ChunkSizeOverflow
    );
}

#[test]
fn rejects_chunk_missing_crlf_after_data() {
    // chunk data must end in CRLF; bare LF rejected
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\n0\r\n\r\n"),
        ErrorKind::MalformedChunkSize
    );
}

#[test]
fn rejects_bad_chunk_extension() {
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5;\x01\r\n"),
        ErrorKind::InvalidChunkExtension
    );
    assert_eq!(
        expect_error(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5;\r\n"),
        ErrorKind::InvalidChunkExtension
    );
}

#[test]
fn rejects_cl_te_framing_fields_in_trailer() {
    let input = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        0\r\nContent-Length: 0\r\n\r\n";
    assert_eq!(expect_error(input), ErrorKind::ForbiddenTrailerField);

    let input = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        0\r\nTrailer: x\r\n\r\n";
    assert_eq!(expect_error(input), ErrorKind::ForbiddenTrailerField);
}

#[test]
fn rejects_short_chunked_body_just_incomplete() {
    // Declared chunk longer than available bytes -> Incomplete, never a frame.
    let mut f = Framer::new();
    let (step, _) = f.step(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhel");
    assert_eq!(step, Step::Incomplete);
}

// ==================================================================
// limits
// ==================================================================

fn small_limits() -> Limits {
    Limits {
        max_request_line_len: 32,
        max_header_section_bytes: 64,
        max_header_count: 3,
        max_body_bytes: 10,
        max_chunk_size_line: 32,
        max_trailer_section_bytes: 64,
        max_trailer_count: 2,
    }
}

#[test]
fn rejects_long_request_line() {
    let input = format!("GET /{} HTTP/1.1\r\n\r\n", "a".repeat(64));
    assert_eq!(
        expect_error_limited(input.as_bytes(), small_limits()),
        ErrorKind::RequestLineTooLong
    );
}

#[test]
fn rejects_large_header_section() {
    let input = format!("GET / HTTP/1.1\r\nX: {}\r\n\r\n", "a".repeat(80));
    assert_eq!(
        expect_error_limited(input.as_bytes(), small_limits()),
        ErrorKind::HeaderSectionTooLarge
    );
}

#[test]
fn rejects_too_many_headers() {
    let input = "GET / HTTP/1.1\r\nA: 1\r\nB: 2\r\nC: 3\r\nD: 4\r\n\r\n".to_string();
    assert_eq!(
        expect_error_limited(input.as_bytes(), small_limits()),
        ErrorKind::TooManyHeaders
    );
}

#[test]
fn rejects_oversized_declared_body() {
    let input = b"POST / HTTP/1.1\r\nContent-Length: 11\r\n\r\n0123456789A";
    assert_eq!(
        expect_error_limited(input, small_limits()),
        ErrorKind::BodyTooLarge
    );
}

#[test]
fn rejects_oversized_chunk() {
    let input =
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0b\r\n0123456789A\r\n0\r\n\r\n";
    assert_eq!(
        expect_error_limited(input, small_limits()),
        ErrorKind::ChunkSizeTooLarge
    );
}

#[test]
fn rejects_oversized_trailer_section() {
    let input = format!(
        "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: {}\r\n\r\n",
        "a".repeat(80)
    );
    assert_eq!(
        expect_error_limited(input.as_bytes(), small_limits()),
        ErrorKind::TrailerSectionTooLarge
    );
}

#[test]
fn rejects_too_many_trailers() {
    let input = "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        0\r\nA: 1\r\nB: 2\r\nC: 3\r\n\r\n";
    assert_eq!(
        expect_error_limited(input.as_bytes(), small_limits()),
        ErrorKind::TooManyTrailers
    );
}

// ==================================================================
// error offsets are deterministic
// ==================================================================

#[test]
fn error_offsets_stable_under_chunking() {
    let input = b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello";
    let mut whole = Framer::new();
    let (Step::Error(e1), _) = whole.step(input) else {
        panic!()
    };
    // The parser anchors the ambiguity at the FIRST Content-Length field —
    // the construct becomes illegal as soon as the second one is seen, but
    // the offending declaration chain starts at the first. The point is that
    // this offset is absolute and identical under every segmentation.
    let expected_offset = 17; // start of the first "Content-Length: 5" line
    assert_eq!(e1.kind, ErrorKind::DuplicateContentLength);
    assert_eq!(e1.offset, expected_offset);

    for cut in 1..input.len() {
        let mut f = Framer::new();
        let (s, _) = f.step(&input[..cut]);
        let err = match s {
            Step::Error(e) => Some(e),
            _ => match f.step(&input[cut..]) {
                (Step::Error(e), _) => Some(e),
                _ => None,
            },
        };
        assert_eq!(err.map(|e| e.offset), Some(e1.offset), "cut {cut}");
        assert_eq!(err.map(|e| e.kind), Some(e1.kind), "cut {cut}");
    }
}
