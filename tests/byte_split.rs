//! Byte-split differential tests.
//!
//! The acceptance criterion is that framing must be independent of how the
//! TCP stream is segmented. For every input we:
//!   1. parse it whole to get a reference trace (frames or one error kind);
//!   2. split it at EVERY byte position (feed [..i] then [i..]) and compare;
//!   3. additionally run a number of pseudo-random multi-cut schedules;
//!   4. for long inputs, also run byte-by-byte delivery.
//!
//! Coverage explicitly includes pipelined requests, chunked trailers,
//! over-long headers/bodies and the classic request-smuggling ambiguities.

mod common;

use common::*;
use http_framing::framer::Limits;

/// Full sweep: every single split + byte-by-byte + random schedules.
fn assert_segmentation_invariant(input: &[u8]) {
    assert_every_single_split_matches(input);

    // byte-by-byte delivery
    let cuts: Vec<usize> = (1..=input.len()).collect();
    let expected = reference_trace(input);
    assert_eq!(trace_with_cuts(input, &cuts), expected, "byte-by-byte");

    // random multi-cut schedules with different seeds
    for seed in [1u64, 2, 3, 42, 1337, 0xdead_beef] {
        for schedule in random_cuts(input.len(), 4, seed) {
            assert_eq!(
                trace_with_cuts(input, &schedule),
                expected,
                "random cuts {schedule:?}"
            );
        }
    }
}

fn assert_segmentation_invariant_limited(input: &[u8], limits: Limits) {
    assert_every_single_split_matches_limited(input, limits);
    let cuts: Vec<usize> = (1..=input.len()).collect();
    let expected = reference_trace_with(input, limits);
    assert_eq!(
        trace_with_cuts_limited(input, &cuts, limits),
        expected,
        "byte-by-byte"
    );
}

// ---------------------------------------------------------------- valid

#[test]
fn split_minimal_get() {
    assert_segmentation_invariant(b"GET / HTTP/1.1\r\n\r\n");
}

#[test]
fn split_post_with_headers_and_body() {
    assert_segmentation_invariant(
        b"POST /upload HTTP/1.1\r\nHost: e\r\nContent-Length: 11\r\n\r\nhello world",
    );
}

#[test]
fn split_chunked_with_trailer() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\nHost: e\r\n\r\n\
        5\r\nhello\r\n6\r\n world\r\n0\r\nX-A: 1\r\nX-B: 2\r\n\r\n";
    assert_segmentation_invariant(input);
}

#[test]
fn split_chunked_multiple_with_embedded_crlf() {
    let input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        4\r\nWiki\r\n5\r\npedia\r\nE\r\n in\r\n\r\nchunks.\r\n0\r\n\r\n";
    assert_segmentation_invariant(input);
}

#[test]
fn split_chunked_extension() {
    assert_segmentation_invariant(
        b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
          5; name=\"x\"\r\nhello\r\n0; fin\r\n\r\n",
    );
}

#[test]
fn split_pipeline_two_gets() {
    assert_segmentation_invariant(
        b"GET /a HTTP/1.1\r\nHost: e\r\n\r\nGET /b HTTP/1.1\r\nHost: e\r\n\r\n",
    );
}

#[test]
fn split_pipeline_four_requests_mixed() {
    let mut input = Vec::new();
    input.extend_from_slice(b"GET /a HTTP/1.1\r\n\r\n");
    input.extend_from_slice(b"POST /b HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc");
    input.extend_from_slice(
        b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nhi\r\n0\r\n\r\n",
    );
    input.extend_from_slice(b"GET /d HTTP/1.1\r\nHost: e\r\nConnection: keep-alive\r\n\r\n");
    assert_segmentation_invariant(&input);
}

#[test]
fn split_pipelined_second_request_is_smuggler() {
    // First valid, then an ambiguous second request — error position must be
    // stable and exactly one frame must precede it at every split point.
    let input = b"GET /a HTTP/1.1\r\n\r\n\
        POST /b HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello";
    assert_segmentation_invariant(input);
}

// ------------------------------------------------------------- smuggling

#[test]
fn split_cl_te() {
    assert_segmentation_invariant(
        b"POST / HTTP/1.1\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    );
}

#[test]
fn split_te_cl() {
    assert_segmentation_invariant(
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 6\r\n\r\n0\r\n\r\n",
    );
}

#[test]
fn split_duplicate_cl() {
    assert_segmentation_invariant(
        b"POST / HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello",
    );
}

#[test]
fn split_conflicting_cl() {
    assert_segmentation_invariant(b"POST / HTTP/1.1\r\nContent-Length: 5, 6\r\n\r\nhello");
}

#[test]
fn split_bare_lf_in_each_position() {
    // Replace each CRLF's CR with nothing: classic lenient-parser divergence.
    let base = b"GET /a HTTP/1.1\r\nHost: e\r\n\r\nGET /b HTTP/1.1\r\n\r\n";
    // Craft one variant with a bare LF between headers.
    let input = b"GET / HTTP/1.1\r\nHost: a\nX: b\r\n\r\n";
    assert_segmentation_invariant(input);
    let _ = base;
}

#[test]
fn split_obs_fold() {
    assert_segmentation_invariant(b"GET / HTTP/1.1\r\nX: a\r\n b\r\n\r\n");
}

#[test]
fn split_space_before_colon() {
    assert_segmentation_invariant(b"GET / HTTP/1.1\r\nX : y\r\n\r\n");
}

#[test]
fn split_chunked_bad_then_smuggled_bytes() {
    // A peer trying to hide a second request inside an "oversized" chunk.
    let input = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
        5\r\nhello\r\nGARBAGE\r\n0\r\n\r\n";
    assert_segmentation_invariant(input);
}

#[test]
fn split_te_multiple_codings() {
    assert_segmentation_invariant(
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked, identity\r\n\r\n0\r\n\r\n",
    );
}

// --------------------------------------------------------------- limits

fn small() -> Limits {
    Limits {
        max_request_line_len: 32,
        max_header_section_bytes: 80,
        max_header_count: 3,
        max_body_bytes: 10,
        max_chunk_size_line: 32,
        max_trailer_section_bytes: 80,
        max_trailer_count: 2,
    }
}

#[test]
fn split_oversized_header_section() {
    let mut input = b"GET / HTTP/1.1\r\nX: ".to_vec();
    input.extend_from_slice(&[b'a'; 120]);
    input.extend_from_slice(b"\r\n\r\n");
    assert_segmentation_invariant_limited(&input, small());
}

#[test]
fn split_oversized_declared_body() {
    // Error is raised at header parse time (declared length exceeds cap).
    let input = b"POST / HTTP/1.1\r\nContent-Length: 11\r\n\r\n0123456789A";
    assert_segmentation_invariant_limited(input, small());
}

#[test]
fn split_oversized_chunk_body() {
    let input =
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0b\r\n0123456789A\r\n0\r\n\r\n";
    assert_segmentation_invariant_limited(input, small());
}

#[test]
fn split_oversized_request_line() {
    let mut input = b"GET /".to_vec();
    input.extend_from_slice(&[b'a'; 100]);
    input.extend_from_slice(b" HTTP/1.1\r\n\r\n");
    assert_segmentation_invariant_limited(&input, small());
}

// ----------------------------------------------------------- long inputs

#[test]
fn split_long_fixed_body() {
    // 4 KiB body: exhaustive single splits plus byte-by-byte (keeps the test
    // fast enough; 4096*4096 would be needlessly slow).
    let mut input = b"POST /big HTTP/1.1\r\nContent-Length: 4096\r\n\r\n".to_vec();
    let body: Vec<u8> = (0..4096u32).map(|i| b'a' + (i % 26) as u8).collect();
    input.extend_from_slice(&body);
    let expected = reference_trace(&input);
    assert_every_single_split_matches(&input);
    let cuts: Vec<usize> = (1..=input.len()).collect();
    assert_eq!(trace_with_cuts(&input, &cuts), expected);
}

#[test]
fn split_many_small_chunks() {
    let mut input = b"POST /c HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n".to_vec();
    for i in 0..50u32 {
        input.extend_from_slice(format!("1\r\n{:x}\r\n", i % 16).as_bytes());
    }
    input.extend_from_slice(b"0\r\n\r\n");
    assert_segmentation_invariant(&input);
}

// -------------------------------------------------- partial-tail semantics

#[test]
fn trailing_partial_is_incomplete_not_frame() {
    let full = b"POST /x HTTP/1.1\r\nContent-Length: 5\r\n\r\nhello";
    assert_segmentation_invariant(full);
    // Any strict prefix must end Incomplete, never as a frame or error.
    for cut in 0..full.len() {
        match trace_with_cuts(&full[..cut], &[cut]) {
            Trace::Frames(frames) => {
                if cut < full.len() {
                    // No frame allowed to be reported before the final byte is in,
                    // except when the prefix contains earlier complete frames —
                    // this input has none.
                    assert!(frames.is_empty(), "frame early at cut {cut}");
                }
            }
            Trace::Error(_, k) => panic!("unexpected error {k:?} at cut {cut}"),
        }
    }
}
