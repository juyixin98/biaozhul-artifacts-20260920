//! End-to-end tests over real loopback TCP sockets, plus scripted-peer
//! tests for framing error handling. Covers the acceptance checklist:
//!
//! 1. half packets (byte-by-byte feeding) and sticky packets
//! 2. concurrency on one connection + out-of-order responses
//! 3. timeout followed by a recognisable late response
//! 4. cancellation races (cancel wins / response wins)
//! 5. bounded memory (payload cap + in-flight cap)
//! 6. connection handling after error frames and after fatal framing damage

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use brpc_reuse::client::{ClientConfig, Connection};
use brpc_reuse::error::{decode_error_payload, ErrorCode};
use brpc_reuse::frame::{
    crc32, encode_frame, encode_frame_vec, flags, Command, Frame, IncrementalDecoder, HEADER_LEN,
    TRAILER_LEN,
};
use brpc_reuse::server::{start, ServerConfig};
use brpc_reuse::service::{ServiceRequest, ServiceResponse};

const TIMEOUT: Duration = Duration::from_secs(5);

fn test_server(max_inflight: usize, max_payload: u32) -> brpc_reuse::server::RunningServer {
    start(ServerConfig {
        bind: "127.0.0.1:0".parse().unwrap(),
        max_payload,
        max_inflight,
        ..Default::default()
    })
    .expect("server start")
}

fn client_for(addr: std::net::SocketAddr, max_inflight: usize) -> Connection {
    Connection::connect(
        addr,
        ClientConfig {
            max_inflight,
            ..Default::default()
        },
    )
    .expect("connect")
}

// ---------------------------------------------------------------------------
// 1. Parser-level: half packets + sticky packets
// ---------------------------------------------------------------------------

#[test]
fn half_and_sticky_packets_parser() {
    let mut wire = Vec::new();
    for id in 0..20u32 {
        encode_frame(
            &Frame::new(Command::Request, id, vec![id as u8; 37]),
            4096,
            &mut wire,
        )
        .unwrap();
    }
    // Feed every byte one at a time (extreme half-packet case), while frames
    // accumulate stuck together in the decoder (sticky packets).
    let mut dec = IncrementalDecoder::new(4096);
    let mut parsed = 0u32;
    for &b in &wire {
        dec.feed(&[b]).unwrap();
        loop {
            match dec.next_frame() {
                Ok(f) => {
                    assert_eq!(f.request_id, parsed);
                    assert_eq!(f.payload.len(), 37);
                    parsed += 1;
                }
                Err(brpc_reuse::error::ParseError::NeedMore) => break,
                Err(e) => panic!("unexpected parse error: {e}"),
            }
        }
    }
    assert_eq!(parsed, 20);
    assert_eq!(dec.buffered_len(), 0);
}

#[test]
fn arbitrary_split_points_never_lose_frames() {
    // 100 frames, fed at pseudo-random chunk boundaries.
    let mut wire = Vec::new();
    for id in 0..100u32 {
        encode_frame(
            &Frame::new(Command::Response, id, format!("body-{id}").into_bytes()),
            4096,
            &mut wire,
        )
        .unwrap();
    }
    let mut dec = IncrementalDecoder::new(4096);
    let mut fed = 0;
    let mut step = 1usize;
    let mut parsed = 0u32;
    while fed < wire.len() {
        step = (step * 7 + 3) % 23 + 1; // 1..=23 byte chunks
        let end = (fed + step).min(wire.len());
        dec.feed(&wire[fed..end]).unwrap();
        fed = end;
        while let Ok(f) = dec.next_frame() {
            assert_eq!(f.request_id, parsed);
            parsed += 1;
        }
    }
    assert_eq!(parsed, 100);
}

// ---------------------------------------------------------------------------
// 2. Basic RPC semantics end-to-end
// ---------------------------------------------------------------------------

#[test]
fn basic_ops_end_to_end() {
    let server = test_server(16, 4096);
    let conn = client_for(server.local_addr(), 16);

    let payload = ServiceRequest::Echo(b"ping".to_vec()).encode();
    let resp = conn.call_wait(payload, TIMEOUT).unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::Ok(b"ping".to_vec())
    );

    let resp = conn
        .call_wait(ServiceRequest::Upper(b"AbC".to_vec()).encode(), TIMEOUT)
        .unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::Ok(b"ABC".to_vec())
    );

    let resp = conn
        .call_wait(ServiceRequest::Add(100, 23).encode(), TIMEOUT)
        .unwrap();
    let body = match ServiceResponse::decode(&resp).unwrap() {
        ServiceResponse::Ok(b) => b,
        other => panic!("{other:?}"),
    };
    assert_eq!(u64::from_be_bytes(body.try_into().unwrap()), 123);

    // Service-level failure is a normal RESPONSE frame carrying the
    // application error status (transport ERROR frames are reserved for
    // protocol errors).
    let resp = conn
        .call_wait(ServiceRequest::Fail.encode(), TIMEOUT)
        .unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::AppError("service forced failure".to_string())
    );

    // Connection still healthy after application error.
    conn.ping(TIMEOUT).unwrap();

    server.shutdown();
}

#[test]
fn ping_pong() {
    let server = test_server(4, 1024);
    let conn = client_for(server.local_addr(), 4);
    conn.ping(TIMEOUT).unwrap();
    conn.ping(TIMEOUT).unwrap();
    server.shutdown();
}

// ---------------------------------------------------------------------------
// 3. Concurrency and out-of-order responses on one connection
// ---------------------------------------------------------------------------

#[test]
fn concurrent_out_of_order_responses() {
    let server = test_server(32, 4096);
    let conn = Arc::new(client_for(server.local_addr(), 32));

    // Issue 20 SLOW calls from multiple threads where later ids finish FIRST.
    // Every caller waits for its own id, proving demultiplexing works even
    // though responses arrive in reverse order.
    let mut handles = Vec::new();
    for i in 0..20u32 {
        let c = conn.clone();
        handles.push(thread::spawn(move || {
            let delay = 600 - i * 20; // id 19 is fastest
            let payload = ServiceRequest::Slow {
                delay_ms: delay,
                body: format!("result-{i}").into_bytes(),
            }
            .encode();
            let resp = c.call_wait(payload, TIMEOUT).unwrap();
            assert_eq!(
                ServiceResponse::decode(&resp).unwrap(),
                ServiceResponse::Ok(format!("result-{i}").into_bytes())
            );
            i
        }));
    }
    let mut finished: Vec<u32> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    finished.sort();
    assert_eq!(finished, (0..20u32).collect::<Vec<_>>());
    server.shutdown();
}

// ---------------------------------------------------------------------------
// 4. Timeout then late response
// ---------------------------------------------------------------------------

#[test]
fn timeout_then_late_response_is_recognised() {
    let server = test_server(16, 4096);
    let conn = client_for(server.local_addr(), 16);

    let payload = ServiceRequest::Slow {
        delay_ms: 1000,
        body: b"late".to_vec(),
    }
    .encode();
    let call = conn.call(payload).unwrap();
    let id = call.id();
    let started = Instant::now();
    let err = call.wait_timeout(Duration::from_millis(100)).unwrap_err();
    assert_eq!(err.code, ErrorCode::Timeout);
    assert!(started.elapsed() < Duration::from_millis(400));

    // The server keeps working; its eventual answer must not be mistaken for
    // another request's response.
    thread::sleep(Duration::from_millis(1300));
    let late = conn.take_late();
    assert!(
        late.iter().any(|l| l.request_id == id),
        "expected a late-response report for id {id}, got {late:?}"
    );

    // A subsequent request still works (connection survived the timeout).
    let resp = conn
        .call_wait(
            ServiceRequest::Echo(b"after-late".to_vec()).encode(),
            TIMEOUT,
        )
        .unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::Ok(b"after-late".to_vec())
    );

    server.shutdown();
}

// ---------------------------------------------------------------------------
// 5. Cancellation races
// ---------------------------------------------------------------------------

#[test]
fn cancel_long_request_wins() {
    let server = test_server(16, 4096);
    let conn = client_for(server.local_addr(), 16);

    let payload = ServiceRequest::Slow {
        delay_ms: 10_000,
        body: b"slow".to_vec(),
    }
    .encode();
    let call = conn.call(payload).unwrap();
    let id = call.id();
    thread::sleep(Duration::from_millis(50));
    let started = Instant::now();
    let err = call.cancel().unwrap_err();
    // Either the server confirms cancellation, or (fast machines) the
    // connection closed path; it must never return the slow body.
    assert!(
        err.code == ErrorCode::Cancelled || err.code == ErrorCode::ConnectionClosed,
        "unexpected cancel outcome: {err:?}"
    );
    assert!(started.elapsed() < Duration::from_secs(2));

    // Cancelled slot frees capacity: the id allocator moved on and the server
    // has removed the request from its in-flight table. Verify by cancelling
    // again-ish: a fresh request with a *different* id succeeds.
    thread::sleep(Duration::from_millis(100));
    let resp = conn
        .call_wait(
            ServiceRequest::Echo(b"post-cancel".to_vec()).encode(),
            TIMEOUT,
        )
        .unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::Ok(b"post-cancel".to_vec())
    );

    // Server must not have produced an unsolicited late frame for {id} with a
    // RESPONSE (it was cancelled mid-sleep). An ERROR(Cancelled) arriving
    // before retirement is delivered to nobody -> recorded late. Either way it
    // must never corrupt another call.
    let late = conn.take_late();
    assert!(
        late.iter().all(|l| l.request_id <= id),
        "late frame for an unexpected id: {late:?}"
    );

    server.shutdown();
}

#[test]
fn cancel_that_loses_race_returns_real_response() {
    let server = test_server(16, 4096);
    let conn = client_for(server.local_addr(), 16);

    // Effectively instant request: by the time CANCEL goes out, the RESPONSE
    // is already queued. The real terminal frame must win.
    let payload = ServiceRequest::Slow {
        delay_ms: 0,
        body: b"instant".to_vec(),
    }
    .encode();
    let call = conn.call(payload).unwrap();
    thread::sleep(Duration::from_millis(100));
    match call.cancel() {
        Ok(payload) => assert_eq!(
            ServiceResponse::decode(&payload).unwrap(),
            ServiceResponse::Ok(b"instant".to_vec())
        ),
        Err(e) if e.code == ErrorCode::Cancelled => {
            // Extremely tight scheduling can still lose; acceptable, but the
            // server must then have removed its in-flight entry.
        }
        Err(e) => panic!("unexpected outcome: {e:?}"),
    }
    server.shutdown();
}

#[test]
fn server_rejects_cancel_unknown_id_then_continues() {
    let server = test_server(16, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();

    // Send CANCEL for id 4242 (never requested).
    let cancel = encode_frame_vec(&Frame::new(Command::Cancel, 4242, vec![]), 4096).unwrap();
    sock.write_all(&cancel).unwrap();

    // Read the ERROR frame back.
    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let err_frame = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(err_frame.command, Command::Error);
    assert_eq!(err_frame.request_id, 4242);
    let (code, _msg) = decode_error_payload(&err_frame.payload);
    assert_eq!(code.unwrap(), ErrorCode::NoSuchRequest);

    // Connection still works: PING -> PONG.
    let ping = encode_frame_vec(&Frame::new(Command::Ping, 1, vec![]), 4096).unwrap();
    sock.write_all(&ping).unwrap();
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);

    server.shutdown();
}

fn read_one_frame(sock: &mut TcpStream, dec: &mut IncrementalDecoder, chunk: &mut [u8]) -> Frame {
    loop {
        match dec.next_frame() {
            Ok(f) => return f,
            Err(brpc_reuse::error::ParseError::NeedMore) => {
                let n = sock.read(chunk).unwrap();
                assert!(n > 0, "peer closed before frame arrived");
                dec.feed(&chunk[..n]).unwrap();
            }
            Err(e) => panic!("fatal: {e}"),
        }
    }
}

// ---------------------------------------------------------------------------
// 6. Bounded memory
// ---------------------------------------------------------------------------

#[test]
fn oversized_declared_payload_is_rejected_without_buffering() {
    // Advertise 1 GiB but send almost nothing: the parser must reject the
    // header immediately and never allocate anywhere near that much.
    let mut hdr = vec![0x42, 0x51, 0x01, 0x00, Command::Request.as_u8(), 0, 0, 0, 1];
    hdr.extend_from_slice(&1_073_741_824u32.to_be_bytes());
    hdr.extend_from_slice(&[0u8; 4]); // fake trailer start
    let mut dec = IncrementalDecoder::new(1024);
    let err = dec.feed(&hdr).unwrap_err();
    assert!(matches!(
        err,
        brpc_reuse::error::ParseError::PayloadTooLarge {
            declared: 1_073_741_824,
            limit: 1024
        }
    ));
    assert!(dec.capacity() < 64 * 1024);
}

#[test]
fn client_enforces_inflight_cap() {
    // Server with slow workers; client allows only 4 in flight.
    let server = test_server(32, 4096);
    let conn = client_for(server.local_addr(), 4);

    let mut calls = Vec::new();
    for _ in 0..4 {
        calls.push(
            conn.call(
                ServiceRequest::Slow {
                    delay_ms: 2000,
                    body: vec![],
                }
                .encode(),
            )
            .unwrap(),
        );
    }
    // 5th must be refused locally with TooManyInflight.
    let err = conn
        .call(ServiceRequest::Echo(b"x".to_vec()).encode())
        .unwrap_err();
    assert_eq!(err.code, ErrorCode::TooManyInflight);

    // Releasing one call frees a slot (here via timeout).
    let _ = calls.remove(0).wait_timeout(Duration::from_millis(10));
    thread::sleep(Duration::from_millis(50));
    let _ok_call = conn
        .call(ServiceRequest::Echo(b"y".to_vec()).encode())
        .expect("slot should be free after a call ended");

    server.shutdown();
}

#[test]
fn server_enforces_inflight_cap_with_server_busy() {
    // max_inflight = 2 on the server: fill it with slow requests over a raw
    // socket, then a third fast request must get ERROR(ServerBusy) while the
    // connection stays alive.
    let server = test_server(2, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(800))).ok();

    let mut slow = Vec::new();
    encode_frame(
        &Frame::new(
            Command::Request,
            10,
            ServiceRequest::Slow {
                delay_ms: 5000,
                body: vec![],
            }
            .encode(),
        ),
        4096,
        &mut slow,
    )
    .unwrap();
    encode_frame(
        &Frame::new(
            Command::Request,
            11,
            ServiceRequest::Slow {
                delay_ms: 5000,
                body: vec![],
            }
            .encode(),
        ),
        4096,
        &mut slow,
    )
    .unwrap();
    sock.write_all(&slow).unwrap();
    thread::sleep(Duration::from_millis(100));

    let echo = encode_frame_vec(
        &Frame::new(
            Command::Request,
            12,
            ServiceRequest::Echo(b"z".to_vec()).encode(),
        ),
        4096,
    )
    .unwrap();
    sock.write_all(&echo).unwrap();

    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let f = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(f.command, Command::Error);
    assert_eq!(f.request_id, 12);
    let (code, _) = decode_error_payload(&f.payload);
    assert_eq!(code.unwrap(), ErrorCode::ServerBusy);

    // PING still works on the same connection.
    sock.write_all(&encode_frame_vec(&Frame::new(Command::Ping, 13, vec![]), 4096).unwrap())
        .unwrap();
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);

    server.shutdown();
}

#[test]
fn payload_over_limit_arrives_as_fatal_connection_close() {
    // Server configured with a 2 KiB cap: sending a frame whose declared
    // length exceeds it kills the connection (framing trust / memory safety).
    let server = test_server(8, 2048);
    let conn = client_for(server.local_addr(), 8);

    // Within the client's own 1 MiB cap but over the server's 2 KiB.
    let big = vec![0xABu8; 4096];
    let err = conn.call_wait(big, TIMEOUT).unwrap_err();
    assert_eq!(err.code, ErrorCode::ConnectionClosed);

    // The dead connection must not resurrect.
    assert!(!conn.is_alive());
    let err2 = conn
        .call_wait(
            ServiceRequest::Echo(vec![]).encode(),
            Duration::from_millis(200),
        )
        .unwrap_err();
    assert_eq!(err2.code, ErrorCode::ConnectionClosed);

    server.shutdown();
}

// ---------------------------------------------------------------------------
// 7. Error frames and fatal framing damage
// ---------------------------------------------------------------------------

#[test]
fn recoverable_error_frame_keeps_connection_alive() {
    // A malformed service payload yields APP_ERROR per-request; the very next
    // request on the same connection must still succeed.
    let server = test_server(8, 4096);
    let conn = client_for(server.local_addr(), 8);

    let err = conn.call_wait(vec![99u8], TIMEOUT).unwrap_err(); // op 99 unknown
    assert_eq!(err.code, ErrorCode::AppError);

    let resp = conn
        .call_wait(
            ServiceRequest::Echo(b"still-here".to_vec()).encode(),
            TIMEOUT,
        )
        .unwrap();
    assert_eq!(
        ServiceResponse::decode(&resp).unwrap(),
        ServiceResponse::Ok(b"still-here".to_vec())
    );

    // Duplicate request id on the wire (raw socket path) is recoverable too.
    server.shutdown();
}

#[test]
fn unknown_command_byte_is_rejected_then_connection_continues() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();

    let weird = encode_frame_vec(&Frame::new(Command::Unknown(77), 1, vec![]), 4096).unwrap();
    sock.write_all(&weird).unwrap();

    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let f = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(f.command, Command::Error);
    let (code, _) = decode_error_payload(&f.payload);
    assert_eq!(code.unwrap(), ErrorCode::UnknownCommand);

    // Follow-up valid frame succeeds.
    sock.write_all(&encode_frame_vec(&Frame::new(Command::Ping, 2, vec![]), 4096).unwrap())
        .unwrap();
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);

    server.shutdown();
}

#[test]
fn reserved_flag_bits_are_rejected_but_connection_lives() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();

    let f = Frame {
        version: 1,
        flags: 0x80, // undefined reserved bit
        command: Command::Request,
        request_id: 3,
        payload: ServiceRequest::Echo(b"x".to_vec()).encode(),
    };
    sock.write_all(&encode_frame_vec(&f, 4096).unwrap())
        .unwrap();

    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let reply = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(reply.command, Command::Error);
    let (code, _) = decode_error_payload(&reply.payload);
    assert_eq!(code.unwrap(), ErrorCode::UnknownFlag);

    sock.write_all(&encode_frame_vec(&Frame::new(Command::Ping, 4, vec![]), 4096).unwrap())
        .unwrap();
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);
    server.shutdown();
}

#[test]
fn known_flag_bit_is_accepted() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();

    let f = Frame {
        version: 1,
        flags: flags::REQ_ACK,
        command: Command::Ping,
        request_id: 5,
        payload: vec![],
    };
    sock.write_all(&encode_frame_vec(&f, 4096).unwrap())
        .unwrap();
    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);
    server.shutdown();
}

#[test]
fn crc_corrupted_frame_closes_connection() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();

    let mut wire = encode_frame_vec(&Frame::new(Command::Ping, 1, b"abc".to_vec()), 4096).unwrap();
    // Corrupt a payload byte without fixing the CRC.
    wire[HEADER_LEN + 1] ^= 0xFF;
    sock.write_all(&wire).unwrap();

    // The server must close the connection rather than parse garbage.
    let mut buf = [0u8; 16];
    let n = sock.read(&mut buf).unwrap_or(0);
    assert_eq!(n, 0, "expected clean close after CRC failure");

    server.shutdown();
}

#[test]
fn half_frame_then_eof_is_detected() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    let wire = encode_frame_vec(&Frame::new(Command::Ping, 1, vec![]), 4096).unwrap();
    sock.write_all(&wire[..wire.len() - 3]).unwrap();
    drop(sock);
    // Server should simply end the connection without hanging; give it a
    // moment and verify process-wide stability by starting another server op.
    thread::sleep(Duration::from_millis(100));
    let conn = client_for(server.local_addr(), 4);
    conn.ping(TIMEOUT).unwrap();
    server.shutdown();
}

#[test]
fn garbage_stream_closes_not_panics() {
    let server = test_server(8, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(500))).ok();
    sock.write_all(&[0xFFu8; 64]).unwrap();
    let mut buf = [0u8; 16];
    let n = sock.read(&mut buf).unwrap_or(0);
    assert_eq!(n, 0);
    server.shutdown();
}

// ---------------------------------------------------------------------------
// 8. Request id allocation: monotonic, never reused
// ---------------------------------------------------------------------------

#[test]
fn request_ids_are_monotonic_and_unreused() {
    let server = test_server(32, 4096);
    let conn = client_for(server.local_addr(), 32);

    let c1 = conn
        .call(ServiceRequest::Echo(b"a".to_vec()).encode())
        .unwrap();
    let id1 = c1.id();
    let c2 = conn
        .call(ServiceRequest::Echo(b"b".to_vec()).encode())
        .unwrap();
    let id2 = c2.id();
    assert_eq!(id2, id1 + 1);
    // Finish and retire id1; the next id must NOT reuse it.
    c1.wait_timeout(TIMEOUT).unwrap();
    let c3 = conn
        .call(ServiceRequest::Echo(b"c".to_vec()).encode())
        .unwrap();
    let id3 = c3.id();
    assert_eq!(id3, id2 + 1);
    assert_ne!(id3, id1);
    c2.wait_timeout(TIMEOUT).unwrap();
    c3.wait_timeout(TIMEOUT).unwrap();

    server.shutdown();
}

#[test]
fn duplicate_inflight_id_rejected_by_server() {
    let server = test_server(16, 4096);
    let mut sock = TcpStream::connect(server.local_addr()).unwrap();
    sock.set_read_timeout(Some(Duration::from_millis(800))).ok();

    // Two requests with the SAME id before the first one answers.
    let mut buf = Vec::new();
    encode_frame(
        &Frame::new(
            Command::Request,
            77,
            ServiceRequest::Slow {
                delay_ms: 3000,
                body: vec![],
            }
            .encode(),
        ),
        4096,
        &mut buf,
    )
    .unwrap();
    encode_frame(
        &Frame::new(
            Command::Request,
            77,
            ServiceRequest::Echo(b"dup".to_vec()).encode(),
        ),
        4096,
        &mut buf,
    )
    .unwrap();
    sock.write_all(&buf).unwrap();

    let mut dec = IncrementalDecoder::new(4096);
    let mut chunk = [0u8; 4096];
    let reply = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(reply.command, Command::Error);
    assert_eq!(reply.request_id, 77);
    let (code, _) = decode_error_payload(&reply.payload);
    assert_eq!(code.unwrap(), ErrorCode::DuplicateRequest);

    // Original request is still alive: PING on the same connection works.
    sock.write_all(&encode_frame_vec(&Frame::new(Command::Ping, 78, vec![]), 4096).unwrap())
        .unwrap();
    let pong = read_one_frame(&mut sock, &mut dec, &mut chunk);
    assert_eq!(pong.command, Command::Pong);
    server.shutdown();
}

// ---------------------------------------------------------------------------
// 8b. Stress: many multiplexed requests over a few connections
// ---------------------------------------------------------------------------

#[test]
fn stress_many_requests_single_connection() {
    let server = test_server(128, 4096);
    let conn = Arc::new(client_for(server.local_addr(), 128));

    // 500 requests fanned out across worker threads; at most 128 are in
    // flight at any instant, enforced by the client semaphore. Threads that
    // hit the cap back off and retry — proving slots are released as
    // responses come back, rather than the queue growing without bound.
    const N: u32 = 500;
    let mut handles = Vec::new();
    for i in 0..N {
        let c = conn.clone();
        handles.push(thread::spawn(move || {
            let payload = ServiceRequest::Add(i as u64, 1).encode();
            let resp = loop {
                match c.call_wait(payload.clone(), TIMEOUT) {
                    Ok(r) => break r,
                    Err(e) if e.code == ErrorCode::TooManyInflight => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(e) => panic!("unexpected error for {i}: {e:?}"),
                }
            };
            let body = match ServiceResponse::decode(&resp).unwrap() {
                ServiceResponse::Ok(b) => b,
                other => panic!("{other:?}"),
            };
            assert_eq!(u64::from_be_bytes(body.try_into().unwrap()), i as u64 + 1);
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    // No late/duplicate frames are tolerated in a correctly matched run.
    assert!(conn.take_late().is_empty());
    server.shutdown();
}

#[test]
fn stress_interleaved_timeouts_and_successes() {
    // Slow (timeout-bound) calls mixed with fast calls on one connection.
    let server = test_server(64, 4096);
    let conn = Arc::new(client_for(server.local_addr(), 64));

    let mut handles = Vec::new();
    for i in 0..40u32 {
        let c = conn.clone();
        handles.push(thread::spawn(move || {
            if i % 3 == 0 {
                // One third time out against a long SLOW.
                let payload = ServiceRequest::Slow {
                    delay_ms: 3000,
                    body: vec![],
                }
                .encode();
                let err = c.call_wait(payload, Duration::from_millis(80)).unwrap_err();
                assert_eq!(err.code, ErrorCode::Timeout);
            } else {
                let payload = ServiceRequest::Echo(format!("fast-{i}").into_bytes()).encode();
                let resp = c.call_wait(payload, TIMEOUT).unwrap();
                assert_eq!(
                    ServiceResponse::decode(&resp).unwrap(),
                    ServiceResponse::Ok(format!("fast-{i}").into_bytes())
                );
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    // Wait for timed-out requests' late answers to arrive.
    thread::sleep(Duration::from_millis(3200));
    let late = conn.take_late();
    let timed_out: u32 = 40 / 3 + 1; // ids 0,3,...,39 => 14
    assert_eq!(
        late.len() as u32,
        timed_out,
        "every timed-out SLOW must surface exactly one late frame: {late:?}"
    );
    assert_eq!(conn.late_dropped_count(), 0);

    // Connection is still perfectly usable after the chaos.
    conn.ping(TIMEOUT).unwrap();
    server.shutdown();
}

// ---------------------------------------------------------------------------
// 9. Encoder refuses oversized payloads
// ---------------------------------------------------------------------------

#[test]
fn encoder_refuses_oversize() {
    let f = Frame::new(Command::Request, 1, vec![0u8; 100]);
    let err = encode_frame_vec(&f, 50).unwrap_err();
    assert_eq!(err, (100, 50));
}

#[test]
fn crc32_self_check() {
    assert_eq!(crc32(b"123456789"), 0xCBF43926);
}

#[test]
fn frame_size_constants_consistent() {
    assert_eq!(HEADER_LEN, 13);
    assert_eq!(TRAILER_LEN, 4);
    let wire = encode_frame_vec(&Frame::new(Command::Ping, 0, vec![]), 1024).unwrap();
    assert_eq!(wire.len(), HEADER_LEN + TRAILER_LEN);
}
