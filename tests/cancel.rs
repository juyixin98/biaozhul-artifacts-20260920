//! Timeout-then-late-response, cancellation races and request-id non-reuse.

mod common;

use std::time::{Duration, Instant};

use brpc::client::{id_parts, ClientConfig, MuxClient, WaitError};
use brpc::payload::{decode_response, encode_request, AppCode, Method};

#[test]
fn late_response_after_timeout_is_identified_and_dropped() {
    let srv = common::test_server();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    let call = c
        .start_request(encode_request(Method::Slow, &common::delay(120, b"late")))
        .unwrap();
    let id = call.id();

    // Give up waiting long before the server finishes.
    let t = Instant::now();
    match call.wait_timeout(Duration::from_millis(30)) {
        Err(WaitError::Timeout) => {}
        other => panic!("expected timeout, got {other:?}"),
    }
    assert!(t.elapsed() < Duration::from_millis(100));
    drop(call); // release the slot; id must never be reused as-is

    // The server response lands ~90 ms later with no current owner.
    assert!(common::wait_until(Duration::from_secs(2), || {
        c.late_responses() >= 1
    }));
    assert_eq!(c.late_responses(), 1);
    let _ = id;

    // The connection is still fully usable after the late response.
    let f = c
        .start_request(encode_request(Method::Echo, b"post-late"))
        .unwrap()
        .wait()
        .unwrap();
    assert_eq!(
        decode_response(&f.payload).unwrap(),
        (AppCode::Ok, b"post-late".as_slice())
    );
}

#[test]
fn request_id_is_never_reused_until_released_and_generation_advances() {
    let srv = common::test_server_large_inflight();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    let first = c.start_request(encode_request(Method::Echo, b"a")).unwrap();
    let id1 = first.id();
    // While outstanding, a newly allocated id must differ.
    let second = c.start_request(encode_request(Method::Echo, b"b")).unwrap();
    let id2 = second.id();
    assert_ne!(id1, id2);

    let f1 = first.wait().unwrap();
    drop(f1);
    let f2 = second.wait().unwrap();
    drop(f2);

    // Slots may be recycled, but the generation advances and the *full*
    // 64-bit id is never handed out again. (Which free slot is popped is an
    // implementation detail; both released slots carry generation 2.)
    let third = c.start_request(encode_request(Method::Echo, b"c")).unwrap();
    let id3 = third.id();
    assert_ne!(id3, id1);
    assert_ne!(id3, id2);

    let (slot3, gen3) = id_parts(id3);
    assert!(slot3 < 2, "must recycle an existing slot, not grow");
    assert_eq!(gen3, 2, "recycled slot gets a fresh generation");
    let _ = third.wait().unwrap();
}

#[test]
fn cancel_wins_race_for_long_running_request() {
    let srv = common::test_server();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    let call = c
        .start_request(encode_request(Method::Slow, &common::delay(500, b"x")))
        .unwrap();
    // Let the server actually start the slow handler.
    std::thread::sleep(Duration::from_millis(30));
    let resp = call.cancel().unwrap();
    let (code, _) = decode_response(&resp.payload).unwrap();
    assert_eq!(code, AppCode::Cancelled);

    assert!(common::wait_until(Duration::from_secs(2), || {
        srv.stats().cancelled >= 1
    }));
}

#[test]
fn repeated_cancel_race_always_resolves_exactly_once() {
    // Hammer the cancel/response race: every call terminates with exactly one
    // outcome — Cancelled or Ok — never duplicated, never stuck.
    let srv = common::test_server_large_inflight();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    const N: usize = 40;
    let (tx, rx) = std::sync::mpsc::channel();
    let mut handles = Vec::new();
    for i in 0..N {
        let c2 = c.clone();
        let tx = tx.clone();
        handles.push(std::thread::spawn(move || {
            // Delays straddle the cancel time so both outcomes occur.
            let delay_ms = 20 + (i as u32 % 8) * 15; // 20..125ms
            let call = c2
                .start_request(encode_request(Method::Slow, &common::delay(delay_ms, b"r")))
                .unwrap();
            std::thread::sleep(Duration::from_millis(20 + (i as u64 % 5) * 12));
            let resp = call.cancel().unwrap();
            let (code, _) = decode_response(&resp.payload).unwrap();
            tx.send(matches!(code, AppCode::Cancelled)).unwrap();
        }));
    }
    drop(tx);
    for h in handles {
        h.join().unwrap();
    }
    let outcomes: Vec<bool> = rx.iter().collect();
    assert_eq!(outcomes.len(), N);

    let cancelled = outcomes.iter().filter(|x| **x).count();
    let completed = N - cancelled;
    // With chosen timings both sides of the race should be exercised.
    assert!(cancelled > 0, "expected at least some cancellations to win");
    assert!(completed > 0, "expected at least some responses to win");

    // No leaks: every call released its slot.
    assert!(common::wait_until(Duration::from_secs(2), || {
        c.inflight_count() == 0
    }));
    assert_eq!(c.inflight_count(), 0);

    // Server accounting: ok + cancelled must cover every request.
    let st = srv.stats();
    assert!(st.responses_ok + st.cancelled >= N);
}

#[test]
fn cancel_after_response_already_local_returns_that_response() {
    let srv = common::test_server();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    // A request that finishes before cancel is invoked. Because the response
    // is already parked in the call's channel, `cancel` returns it and never
    // puts a CANCEL frame on the wire.
    let call = c
        .start_request(encode_request(Method::Echo, b"fast"))
        .unwrap();
    std::thread::sleep(Duration::from_millis(80));
    let resp = call.cancel().unwrap();
    let (code, body) = decode_response(&resp.payload).unwrap();
    assert_eq!(code, AppCode::Ok);
    assert_eq!(body, b"fast");

    // Connection still healthy.
    let f = c
        .start_request(encode_request(Method::Echo, b"again"))
        .unwrap()
        .wait()
        .unwrap();
    assert_eq!(
        decode_response(&f.payload).unwrap(),
        (AppCode::Ok, b"again".as_slice())
    );
}
