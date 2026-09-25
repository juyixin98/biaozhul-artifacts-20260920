//! Bounded-memory / backpressure guarantees.

mod common;

use std::time::Duration;

use brpc::client::{ClientConfig, ClientError, MuxClient};
use brpc::payload::{decode_response, encode_request, Method};

#[test]
fn client_enforces_inflight_cap_with_backpressure() {
    let srv = common::test_server_large_inflight();
    // Client cap is intentionally tiny (3).
    let conn = MuxClient::connect_with(
        srv.local_addr(),
        ClientConfig {
            max_inflight: 3,
            ..ClientConfig::default()
        },
    )
    .unwrap();
    let c = conn.client();

    let mut held = Vec::new();
    for _ in 0..3 {
        held.push(
            c.start_request(encode_request(Method::Slow, &common::delay(400, b"h")))
                .unwrap(),
        );
    }
    assert_eq!(c.inflight_count(), 3);

    // Fourth must be refused with backpressure, not silently buffered.
    match c.start_request(encode_request(Method::Echo, b"overflow")) {
        Err(ClientError::TooManyInFlight) => {}
        Err(e) => panic!("expected TooManyInFlight, got {e}"),
        Ok(_) => panic!("expected TooManyInFlight, but request was accepted"),
    }
    assert_eq!(c.inflight_count(), 3);

    // Release one; a slot frees and a new request is accepted.
    let first = held.remove(0);
    // Don't care about its response for this assertion; cancel to release.
    let _ = first.cancel();
    assert!(common::wait_until(Duration::from_secs(2), || {
        c.inflight_count() <= 2
    }));

    let again = c.start_request(encode_request(Method::Echo, b"fits now"));
    assert!(again.is_ok(), "slot should be available after release");
    let _ = again.unwrap().wait();
}

#[test]
fn server_rejects_over_inflight_with_app_frame_and_keeps_connection() {
    // Use a permissive client cap but a tiny server cap (server test cfg uses
    // max_inflight = 8). Fire 8 slow requests, then one more; the server must
    // answer it TooManyInFlight inside a normal frame while the others run.
    let srv = common::test_server();
    let conn = MuxClient::connect_with(
        srv.local_addr(),
        ClientConfig {
            max_inflight: 64,
            ..ClientConfig::default()
        },
    )
    .unwrap();
    let c = conn.client();

    let mut slow = Vec::new();
    for _ in 0..8 {
        slow.push(
            c.start_request(encode_request(Method::Slow, &common::delay(300, b"s")))
                .unwrap(),
        );
    }
    // The 9th should be rejected server-side with an application code.
    let rejected = c
        .start_request(encode_request(Method::Echo, b"nine"))
        .unwrap()
        .wait()
        .unwrap();
    let (code, _) = decode_response(&rejected.payload).unwrap();
    assert_eq!(code, brpc::payload::AppCode::TooManyInFlight);

    // Drain the slow ones; connection is healthy.
    for call in slow {
        let f = call.cancel();
        assert!(f.is_ok());
    }
    let f = c
        .start_request(encode_request(Method::Echo, b"healthy"))
        .unwrap()
        .wait()
        .unwrap();
    assert_eq!(
        decode_response(&f.payload).unwrap(),
        (brpc::payload::AppCode::Ok, b"healthy".as_slice())
    );
    assert!(srv.stats().rejected_inflight >= 1);
}

#[test]
fn long_run_keeps_inflight_table_empty_between_bursts() {
    // Bounded-memory regression: after many sequential bursts no slots or
    // channels may leak (inflight returns to 0, late responses stay 0).
    let srv = common::test_server_large_inflight();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    for burst in 0..8 {
        let calls: Vec<_> = (0..20)
            .map(|i| {
                c.start_request(encode_request(
                    Method::Echo,
                    format!("b{burst}-{i}").as_bytes(),
                ))
                .unwrap()
            })
            .collect();
        for call in calls {
            let _ = call.wait().unwrap();
        }
        assert_eq!(c.inflight_count(), 0, "leak after burst {burst}");
    }
    assert_eq!(c.late_responses(), 0);
}
