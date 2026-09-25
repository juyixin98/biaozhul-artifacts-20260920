//! Same-connection concurrency and out-of-order responses over real TCP.

mod common;

use std::thread;
use std::time::Duration;

use brpc::client::{ClientConfig, MuxClient};
use brpc::payload::{decode_response, encode_request, AppCode, Method};

#[test]
fn echo_and_add_basic() {
    let srv = common::test_server();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    let f = c
        .start_request(encode_request(Method::Echo, b"hello"))
        .unwrap()
        .wait()
        .unwrap();
    let (code, body) = decode_response(&f.payload).unwrap();
    assert_eq!(code, AppCode::Ok);
    assert_eq!(body, b"hello");

    let mut add = 40i64.to_be_bytes().to_vec();
    add.extend_from_slice(&2i64.to_be_bytes());
    let f = c
        .start_request(encode_request(Method::Add, &add))
        .unwrap()
        .wait()
        .unwrap();
    let (_, body) = decode_response(&f.payload).unwrap();
    assert_eq!(body, 42i64.to_be_bytes());
}

#[test]
fn concurrent_requests_on_one_connection_complete_out_of_order() {
    let srv = common::test_server_large_inflight();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    // Start a slow request first, then several fast ones. Responses must
    // arrive keyed by id, regardless of order.
    let slow = c
        .start_request(encode_request(Method::Slow, &common::delay(150, b"S")))
        .unwrap();
    let slow_id = slow.id();

    let mut fast_ids = Vec::new();
    let mut fast_calls = Vec::new();
    for i in 0..5 {
        let call = c
            .start_request(encode_request(Method::Echo, format!("f{i}").as_bytes()))
            .unwrap();
        fast_ids.push(call.id());
        fast_calls.push(call);
    }

    // Drain the fast calls; the slow call is still pending (out-of-order
    // delivery does not block the fast ones behind it).
    for (i, call) in fast_calls.into_iter().enumerate() {
        let f = call.wait().unwrap();
        assert_eq!(f.request_id, fast_ids[i]);
        let (code, body) = decode_response(&f.payload).unwrap();
        assert_eq!((code, body), (AppCode::Ok, format!("f{i}").as_bytes()));
    }

    // Only now collect the slow one.
    assert_eq!(c.inflight_count(), 1);
    let f = slow.wait().unwrap();
    assert_eq!(f.request_id, slow_id);
    let (_, body) = decode_response(&f.payload).unwrap();
    assert_eq!(body, b"S");
    assert_eq!(c.inflight_count(), 0);
}

#[test]
fn many_concurrent_requests_all_routed_correctly() {
    let srv = common::test_server_large_inflight();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    let n = 60;
    let calls: Vec<_> = (0..n)
        .map(|i| {
            let body = format!("payload-{i:03}");
            c.start_request(encode_request(Method::Echo, body.as_bytes()))
                .unwrap()
        })
        .collect();
    for (i, call) in calls.into_iter().enumerate() {
        let f = call.wait().unwrap();
        let (code, body) = decode_response(&f.payload).unwrap();
        assert_eq!(code, AppCode::Ok);
        assert_eq!(body, format!("payload-{i:03}").as_bytes());
    }
    assert_eq!(c.late_responses(), 0);

    // Server saw every request.
    let stats = srv.stats();
    assert!(stats.requests >= n as usize);
}

#[test]
fn application_error_keeps_connection_alive() {
    let srv = common::test_server();
    let conn = MuxClient::connect_with(srv.local_addr(), ClientConfig::default()).unwrap();
    let c = conn.client();

    // add with too-short args => BadArgs inside a perfectly good frame.
    let f = c
        .start_request(encode_request(Method::Add, &[0u8; 4]))
        .unwrap()
        .wait()
        .unwrap();
    let (code, _) = decode_response(&f.payload).unwrap();
    assert_eq!(code, AppCode::BadArgs);

    // Unknown method => UnknownMethod, connection still usable.
    let mut bad_method = 9999u16.to_be_bytes().to_vec();
    bad_method.extend_from_slice(b"x");
    let f = c.start_request(bad_method).unwrap().wait().unwrap();
    let (code, _) = decode_response(&f.payload).unwrap();
    assert_eq!(code, AppCode::UnknownMethod);

    let f = c
        .start_request(encode_request(Method::Echo, b"after errors"))
        .unwrap()
        .wait()
        .unwrap();
    let (code, body) = decode_response(&f.payload).unwrap();
    assert_eq!((code, body), (AppCode::Ok, b"after errors".as_slice()));

    // No framing errors were recorded by the server.
    assert_eq!(srv.stats().framing_errors, 0);
    let _ = Duration::from_millis(1);
    let _ = thread::current();
}
