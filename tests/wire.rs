//! Wire-level tests: half packets, sticky packets and framing-error handling
//! are exercised against the server with a raw TCP socket.

mod common;

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use brpc::decode::FrameDecoder;
use brpc::frame::{Frame, FrameKind};
use brpc::payload::{decode_response, encode_request, AppCode, Method};

fn connect(srv: &brpc::server::Server) -> TcpStream {
    let s = TcpStream::connect(srv.local_addr()).unwrap();
    s.set_read_timeout(Some(Duration::from_millis(300)))
        .unwrap();
    s.set_nodelay(true).unwrap();
    s
}

fn read_one_frame(s: &mut TcpStream, max: usize) -> Frame {
    let mut dec = FrameDecoder::new(max);
    let mut chunk = [0u8; 4096];
    loop {
        let n = s.read(&mut chunk).unwrap();
        assert!(n > 0, "server closed unexpectedly");
        dec.push(&chunk[..n]).unwrap();
        if let Some(f) = dec.next_frame().unwrap() {
            return f;
        }
    }
}

#[test]
fn server_handles_byte_at_a_time_half_packet() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    let wire = Frame::new(FrameKind::Request, 1, encode_request(Method::Echo, b"h2b")).encode();
    // Send every byte in its own write (pathological half packets).
    for b in &wire {
        s.write_all(&[*b]).unwrap();
        s.flush().unwrap();
    }

    let f = read_one_frame(&mut s, 4096);
    let (code, body) = decode_response(&f.payload).unwrap();
    assert_eq!((code, body), (AppCode::Ok, b"h2b".as_slice()));
}

#[test]
fn server_handles_glued_frames() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    // Glue three requests into one TCP segment.
    let mut glued = Vec::new();
    for (id, text) in [(1u64, "g1"), (2, "g2"), (3, "g3")] {
        glued.extend_from_slice(
            &Frame::new(
                FrameKind::Request,
                id,
                encode_request(Method::Echo, text.as_bytes()),
            )
            .encode(),
        );
    }
    s.write_all(&glued).unwrap();
    s.flush().unwrap();

    let mut got = Vec::new();
    for _ in 0..3 {
        let f = read_one_frame(&mut s, 4096);
        got.push((
            f.request_id,
            decode_response(&f.payload).unwrap().1.to_vec(),
        ));
    }
    got.sort_by_key(|(id, _)| *id);
    assert_eq!(
        got,
        vec![
            (1, b"g1".to_vec()),
            (2, b"g2".to_vec()),
            (3, b"g3".to_vec()),
        ]
    );
}

#[test]
fn request_id_zero_is_routed_normally_by_wire_layer() {
    // id 0 is never minted by the client (generations start at 1), but the
    // framing layer treats it as an ordinary id; server replies.
    let srv = common::test_server();
    let mut s = connect(&srv);
    let wire = Frame::new(FrameKind::Request, 0, encode_request(Method::Echo, b"z")).encode();
    s.write_all(&wire).unwrap();
    s.flush().unwrap();
    let f = read_one_frame(&mut s, 4096);
    assert_eq!(f.request_id, 0);
}

#[test]
fn bad_magic_closes_connection_and_counts_framing_error() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    let mut wire = Frame::new(FrameKind::Request, 1, encode_request(Method::Echo, b"x")).encode();
    wire[0] ^= 0xFF; // corrupt magic
    s.write_all(&wire).unwrap();
    s.flush().unwrap();

    // A length-prefixed protocol cannot resync: server must close the conn.
    s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut buf = [0u8; 64];
    let n = s.read(&mut buf).expect("read after bad magic");
    assert_eq!(n, 0, "expected clean EOF, got {} bytes", n);

    assert!(common::wait_until(Duration::from_secs(2), || {
        srv.stats().framing_errors >= 1
    }));
}

#[test]
fn payload_length_over_cap_closes_connection() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    // Declare 1 MiB payload but send the 24-byte header only; server cap is
    // 4096, so it must reject from the header and close — never buffer 1 MiB.
    let f = Frame::new(FrameKind::Request, 1, vec![]);
    let mut wire = f.encode();
    wire[16..20].copy_from_slice(&1_048_576u32.to_be_bytes());
    // Rewrite CRC over header fields + (empty) payload so only length is bad.
    let mut h = brpc::crc32::Crc32::new();
    h.update(&wire[4..20]);
    let crc = h.finalize();
    wire[20..24].copy_from_slice(&crc.to_be_bytes());

    s.write_all(&wire).unwrap();
    s.flush().unwrap();

    s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut buf = [0u8; 64];
    let n = s.read(&mut buf).expect("read after oversize");
    assert_eq!(n, 0);
    assert!(common::wait_until(Duration::from_secs(2), || {
        srv.stats().framing_errors >= 1
    }));
}

#[test]
fn corrupted_crc_closes_connection() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    let mut wire = Frame::new(FrameKind::Request, 1, encode_request(Method::Echo, b"abc")).encode();
    let last = wire.len() - 1;
    wire[last] ^= 0x01;
    s.write_all(&wire).unwrap();
    s.flush().unwrap();

    s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut buf = [0u8; 64];
    assert_eq!(s.read(&mut buf).unwrap(), 0);
}

#[test]
fn partial_header_then_good_frame_works_until_evil_appears() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    // Half a header, paused, then the rest of a *good* frame: parser must not
    // confuse the two.
    let good = Frame::new(FrameKind::Request, 9, encode_request(Method::Echo, b"ok")).encode();
    s.write_all(&good[..10]).unwrap();
    s.flush().unwrap();
    std::thread::sleep(Duration::from_millis(30));
    s.write_all(&good[10..]).unwrap();
    s.flush().unwrap();
    let f = read_one_frame(&mut s, 4096);
    assert_eq!(f.request_id, 9);
}

#[test]
fn cancel_for_unknown_id_is_acknowledged_and_connection_lives() {
    let srv = common::test_server();
    let mut s = connect(&srv);

    // CANCEL for an id the server has never seen: must answer Cancelled in a
    // normal frame and keep serving on the same connection.
    let cancel = Frame::new(FrameKind::Cancel, 0xDEAD_BEEF, vec![]).encode();
    s.write_all(&cancel).unwrap();
    s.flush().unwrap();

    let f = read_one_frame(&mut s, 4096);
    assert_eq!(f.kind, FrameKind::Response);
    assert_eq!(f.request_id, 0xDEAD_BEEF);
    let (code, _) = decode_response(&f.payload).unwrap();
    assert_eq!(code, AppCode::Cancelled);

    assert!(common::wait_until(Duration::from_secs(2), || {
        srv.stats().unknown_cancel >= 1
    }));

    // Follow with a normal request on the same connection.
    let req = Frame::new(
        FrameKind::Request,
        5,
        encode_request(Method::Echo, b"alive"),
    )
    .encode();
    s.write_all(&req).unwrap();
    s.flush().unwrap();
    let f = read_one_frame(&mut s, 4096);
    let (code, body) = decode_response(&f.payload).unwrap();
    assert_eq!((code, body), (AppCode::Ok, b"alive".as_slice()));
}
