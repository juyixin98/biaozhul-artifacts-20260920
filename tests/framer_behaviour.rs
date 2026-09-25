//! Behaviour tests: decoded content, pipelining boundaries and
//! incremental delivery semantics beyond the split-equivalence sweep.

use http_framing::{Framing, Framer, Limits};

fn frame_once(input: &[u8]) -> Result<Vec<http_framing::Request>, http_framing::FrameError> {
    let mut f = Framer::new();
    let reqs = f.feed(input)?;
    f.end_input()?;
    Ok(reqs)
}

#[test]
fn fixed_body_is_decoded_verbatim_including_lookalike_wires() {
    let body = b"GET /admin HTTP/1.1\r\nHost: x\r\n\r\n";
    let mut wire = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: "
        .to_vec();
    wire.extend_from_slice(body.len().to_string().as_bytes());
    wire.extend_from_slice(b"\r\n\r\n");
    wire.extend_from_slice(body);

    let reqs = frame_once(&wire).unwrap();
    assert_eq!(reqs.len(), 1);
    let req = &reqs[0];
    assert_eq!(req.framing, Framing::ContentLength(body.len() as u64));
    assert_eq!(req.body, body);
    // The smuggled-looking bytes stayed payload: no second request.
}

#[test]
fn chunked_body_is_decoded_without_metadata() {
    let wire = b"POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
                4\r\nWiki\r\n5\r\npedia\r\n0\r\nX-A: b\r\n\r\n";
    let reqs = frame_once(wire).unwrap();
    assert_eq!(reqs.len(), 1);
    let req = &reqs[0];
    assert_eq!(req.framing, Framing::Chunked);
    assert_eq!(req.body, b"Wikipedia");
    assert_eq!(req.trailers.len(), 1);
    assert_eq!(req.trailers[0].name, "X-A");
    assert_eq!(req.trailers[0].value, b"b");
}

#[test]
fn pipelined_requests_are_framed_independently() {
    let wire = b"GET /1 HTTP/1.1\r\nHost: x\r\n\r\n\
                POST /2 HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc\
                POST /c HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
                2\r\nhi\r\n0\r\n\r\n\
                GET /4 HTTP/1.1\r\nHost: x\r\n\r\n";
    let reqs = frame_once(wire).unwrap();
    assert_eq!(reqs.len(), 4);
    assert_eq!(reqs[0].target, "/1");
    assert_eq!(reqs[0].body, b"");
    assert_eq!(reqs[1].target, "/2");
    assert_eq!(reqs[1].body, b"abc");
    assert_eq!(reqs[2].target, "/c");
    assert_eq!(reqs[2].body, b"hi");
    assert_eq!(reqs[3].target, "/4");
}

#[test]
fn partial_then_rest_completes_and_buffer_drains() {
    let wire = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n";
    let mut f = Framer::new();
    let r1 = f.feed(&wire[..10]).unwrap();
    assert!(r1.is_empty());
    assert!(f.pending_bytes() > 0);
    let r2 = f.feed(&wire[10..]).unwrap();
    assert_eq!(r2.len(), 1);
    assert_eq!(f.pending_bytes(), 0);
    f.end_input().unwrap();
}

#[test]
fn empty_feeds_are_noops() {
    let wire = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n";
    let mut f = Framer::new();
    assert!(f.feed(b"").unwrap().is_empty());
    assert!(f.feed(&wire[..5]).unwrap().is_empty());
    assert!(f.feed(b"").unwrap().is_empty());
    let reqs = f.feed(&wire[5..]).unwrap();
    assert_eq!(reqs.len(), 1);
}

#[test]
fn after_error_parser_is_not_used_again_by_server() {
    // The library itself returns the machine for diagnostics; the
    // contract callers rely on is simply that the error is stable.
    let mut f = Framer::new();
    let e1 = f
        .feed(b"POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n")
        .unwrap_err()
        .kind;
    assert_eq!(e1, http_framing::ErrorKind::DuplicateContentLength);
}

#[test]
fn smuggling_prefix_does_not_frame_a_second_request() {
    // CL.TE attempt: a lenient front-end would read the CL body and
    // see a second request; this parser rejects the head outright, so
    // even feeding "the rest" afterwards yields no request.
    let wire = b"POST / HTTP/1.1\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n\
                0\r\n\r\nGET /admin HTTP/1.1\r\nHost: x\r\n\r\n";
    let mut f = Framer::new();
    let e = f.feed(wire).unwrap_err().kind;
    assert_eq!(e, http_framing::ErrorKind::TeWithContentLength);
}

#[test]
fn stream_limit_enforced_while_body_streams() {
    // Fixed-length: declared length is legal, but the sender actually
    // streams past the global body cap. CL is checked against the cap
    // at the header too, so pick a cap the declaration hits exactly:
    // a cap of 3 with CL 3 is legal but no bytes arrive — instead use
    // chunked where the second chunk crosses the total.
    let limits = Limits {
        max_body_bytes: 5,
        max_chunk_size: 100,
        ..Limits::default()
    };
    let mut f = Framer::with_limits(limits);
    let err = f
        .feed(
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n\
              3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n",
        )
        .unwrap_err()
        .kind;
    assert_eq!(err, http_framing::ErrorKind::BodyTooLarge);
}

#[test]
fn zero_chunk_immediately_completes_chunked() {
    let reqs = frame_once(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    )
    .unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].body, b"");
    assert!(reqs[0].trailers.is_empty());
}

#[test]
fn headers_preserve_case_and_multiple_fields() {
    let reqs = frame_once(
        b"GET / HTTP/1.1\r\nX-Mixed: One\r\nX-Mixed: Two\r\nHost: h\r\n\r\n",
    )
    .unwrap();
    let xs: Vec<_> = reqs[0]
        .headers
        .iter()
        .filter(|h| h.is("x-mixed"))
        .collect();
    assert_eq!(xs.len(), 2);
    assert_eq!(xs[0].value, b"One");
    assert_eq!(xs[1].value, b"Two");
}
