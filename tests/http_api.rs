//! End-to-end tests of the Axum replay service using in-process requests
//! (tower oneshot) — no network, no mocks: the real router, real parser, real
//! CRC and real encoder run here.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::Value;
use tower::ServiceExt;

use serialframe::parser::{encode_frame, MAGIC0, MAGIC1};
use serialframe::server::{app, hex_decode};

fn test_app() -> axum::Router {
    app(64) // small max_payload to exercise limits cheaply
}

async fn json_of(resp: axum::response::Response) -> (StatusCode, Value) {
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let v: Value = serde_json::from_slice(&bytes).unwrap_or_else(|e| {
        panic!("response is not JSON ({e}): {:?}", String::from_utf8_lossy(&bytes))
    });
    (status, v)
}

async fn post_raw(app: &axum::Router, path: &str, body: Vec<u8>) -> (StatusCode, Value) {
    let req = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/octet-stream")
        .body(Body::from(body))
        .unwrap();
    json_of(app.clone().oneshot(req).await.unwrap()).await
}

async fn get(app: &axum::Router, path: &str) -> (StatusCode, Value) {
    let req = Request::builder().uri(path).body(Body::empty()).unwrap();
    json_of(app.clone().oneshot(req).await.unwrap()).await
}

#[tokio::test]
async fn health_and_root() {
    let app = test_app();
    let (status, v) = get(&app, "/healthz").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["status"], "ok");
    assert_eq!(v["max_payload"], 64);

    let (status, v) = get(&app, "/").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["service"], "serialframe-replay");
}

#[tokio::test]
async fn feed_roundtrip_and_events() {
    let app = test_app();
    let f1 = encode_frame(1, b"hello").unwrap();
    let f2 = encode_frame(2, b"world").unwrap();

    // Half packet: first part of f1.
    let cut = 4;
    let (status, v) = post_raw(&app, "/v1/sessions/s1/feed", f1[..cut].to_vec()).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["events"].as_array().unwrap().len(), 0);
    assert_eq!(v["buffered"], cut as u64);

    // Rest of f1 + all of f2 (sticky).
    let mut rest = f1[cut..].to_vec();
    rest.extend_from_slice(&f2);
    let (status, v) = post_raw(&app, "/v1/sessions/s1/feed", rest).await;
    assert_eq!(status, StatusCode::OK);
    let events = v["events"].as_array().unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["type"], "frame");
    assert_eq!(events[0]["seq"], 1);
    assert_eq!(events[0]["payload_utf8"], "hello");
    assert_eq!(events[1]["seq"], 2);

    // Frames endpoint shows only frames.
    let (status, v) = get(&app, "/v1/sessions/s1/frames").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["frames_total"], 2);
    assert_eq!(v["frames"].as_array().unwrap().len(), 2);

    // Events endpoint with since.
    let (status, v) = get(&app, "/v1/sessions/s1/events?since=1").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["events"].as_array().unwrap().len(), 1);
    assert_eq!(v["events"][0]["seq"], 2);
}

#[tokio::test]
async fn noise_crc_oversize_gap_via_http() {
    let app = test_app();

    // Noise + corrupt frame + oversize + gap, all in one stream.
    let mut stream = vec![0x00, 0xFF, MAGIC0]; // noise
    let mut bad = encode_frame(1, b"bad").unwrap();
    bad[6] ^= 0x55;
    stream.extend_from_slice(&bad);
    // oversize declaration (declared 100 > max 64)
    stream.push(MAGIC0);
    stream.push(MAGIC1);
    stream.extend_from_slice(&100u16.to_be_bytes());
    stream.extend_from_slice(&9u16.to_be_bytes());
    stream.extend_from_slice(&[0x77; 8]);
    // valid frames with a sequence gap: 5 then 8 (missing 6,7)
    stream.extend_from_slice(&encode_frame(5, b"five").unwrap());
    stream.extend_from_slice(&encode_frame(8, b"eight").unwrap());

    let (status, v) = post_raw(&app, "/v1/sessions/mix/feed", stream).await;
    assert_eq!(status, StatusCode::OK);
    let types: Vec<&str> = v["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["type"].as_str().unwrap())
        .collect();
    assert!(types.contains(&"noise"), "{types:?}");
    assert!(types.contains(&"crc_mismatch"), "{types:?}");
    assert!(types.contains(&"oversize"), "{types:?}");
    assert!(types.contains(&"gap"), "{types:?}");
    assert_eq!(
        types.iter().filter(|t| **t == "frame").count(),
        2,
        "{types:?}"
    );
    // Gap details: from 6 to 8, count 2.
    let gap = v["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["type"] == "gap")
        .unwrap();
    assert_eq!(gap["from"], 6);
    assert_eq!(gap["to"], 8);
    assert_eq!(gap["count"], 2);
}

#[tokio::test]
async fn encode_endpoint_matches_library() {
    let app = test_app();
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"seq": 4660, "payload_hex": "deadbeef"}"#))
        .unwrap();
    let (status, v) = json_of(app.oneshot(req).await.unwrap()).await;
    assert_eq!(status, StatusCode::OK);
    let wire = hex_decode(v["bytes_hex"].as_str().unwrap()).unwrap();
    assert_eq!(wire, encode_frame(4660, &[0xDE, 0xAD, 0xBE, 0xEF]).unwrap());
}

#[tokio::test]
async fn encode_rejects_bad_hex_and_oversize() {
    let app = test_app();
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(r#"{"seq": 1, "payload_hex": "zz"}"#))
        .unwrap();
    let (status, _) = json_of(app.clone().oneshot(req).await.unwrap()).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let big = "aa".repeat(65); // 65 bytes > max_payload 64
    let req = Request::builder()
        .method("POST")
        .uri("/v1/encode")
        .header("content-type", "application/json")
        .body(Body::from(format!(r#"{{"seq": 1, "payload_hex": "{big}"}}"#)))
        .unwrap();
    let (status, _) = json_of(app.oneshot(req).await.unwrap()).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
}

#[tokio::test]
async fn unknown_session_and_reset() {
    let app = test_app();
    let (status, _) = get(&app, "/v1/sessions/nope/events").await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let wire = encode_frame(1, b"x").unwrap();
    post_raw(&app, "/v1/sessions/r/feed", wire).await;
    let (status, _) = post_raw(&app, "/v1/sessions/r/reset", Vec::new()).await;
    assert_eq!(status, StatusCode::OK);
    let (status, v) = get(&app, "/v1/sessions/r/events").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(v["events"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn sessions_listed() {
    let app = test_app();
    post_raw(&app, "/v1/sessions/alpha/feed", encode_frame(1, b"a").unwrap()).await;
    post_raw(&app, "/v1/sessions/beta/feed", encode_frame(1, b"b").unwrap()).await;
    let (status, v) = get(&app, "/v1/sessions").await;
    assert_eq!(status, StatusCode::OK);
    let ids: Vec<&str> = v["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, vec!["alpha", "beta"]);
}

#[tokio::test]
async fn body_limit_returns_413() {
    let app = test_app(); // max_payload 64 => parser hard bound 74
    // Larger than one max-size frame but under the router body limit:
    // rejected by the parser's own bound check with a JSON 413.
    let (status, v) = post_raw(&app, "/v1/sessions/big/feed", vec![0u8; 75]).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert!(v["error"].as_str().unwrap().contains("exceeds parser hard bound"));

    // Larger than the router body limit too: still 413 (plain-text axum body).
    let req = Request::builder()
        .method("POST")
        .uri("/v1/sessions/big/feed")
        .body(Body::from(vec![0u8; 10_000]))
        .unwrap();
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::PAYLOAD_TOO_LARGE);
}
