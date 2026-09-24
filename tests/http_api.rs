//! HTTP-level tests using Axum's in-process router (no open network port).

use axum::{
    body::{Body, Bytes},
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use serial_frame_parser::{
    encode_frame, http, sample, DEFAULT_MAX_PAYLOAD,
};
use serde_json::Value;
use tower::util::ServiceExt;

// `http-body-util` is already present via Axum's dependency tree.

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap()
}

async fn body_bytes(resp: axum::response::Response) -> Bytes {
    resp.into_body().collect().await.unwrap().to_bytes()
}

#[tokio::test]
async fn parse_sample_stream_end_to_end() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let data = sample::build(DEFAULT_MAX_PAYLOAD);

    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/parse")
                .header("content-type", "application/octet-stream")
                .body(Body::from(data))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let j = body_json(resp).await;

    let stats = &j["stats"];
    assert_eq!(stats["frames_ok"], 4); // seq 1,2,4,5
    assert_eq!(stats["bad_crc"], 1); // seq 3 corrupted
    assert_eq!(stats["oversized"], 1); // claimed 5000
    assert_eq!(stats["truncated"], 1); // seq 6 cut off

    let types: Vec<&str> = j["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["type"].as_str().unwrap())
        .collect();
    assert!(types.contains(&"frame"));
    assert!(types.contains(&"bad_crc"));
    assert!(types.contains(&"oversized_length"));
    assert!(types.contains(&"truncated"));
    assert!(types.contains(&"noise"));

    let seqs: Vec<u64> = j["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["type"] == "frame")
        .map(|e| e["sequence"].as_u64().unwrap())
        .collect();
    assert_eq!(seqs, vec![1, 2, 4, 5]);
}

#[tokio::test]
async fn encode_then_parse_roundtrip() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let enc_resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/encode")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"sequence":1234,"payload_hex":"deadbeef"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(enc_resp.status(), StatusCode::OK);
    let enc = body_json(enc_resp).await;
    let hex = enc["frame_hex"].as_str().unwrap();
    assert!(hex.starts_with("deadbeef"));

    let bytes = hex_to_bytes(hex);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/parse")
                .body(Body::from(bytes))
                .unwrap(),
        )
        .await
        .unwrap();
    let j = body_json(resp).await;
    let frame = &j["events"][0];
    assert_eq!(frame["type"], "frame");
    assert_eq!(frame["sequence"], 1234);
    assert_eq!(frame["payload_hex"], "deadbeef");
}

#[tokio::test]
async fn stateful_session_reassembles_half_packets_across_requests() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let created = app
        .clone()
        .oneshot(Request::builder().method("POST").uri("/session").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(created.status(), StatusCode::OK);
    let cj = body_json(created).await;
    let id = cj["session_id"].as_str().unwrap().to_string();

    let raw = encode_frame(9, b"across-requests", DEFAULT_MAX_PAYLOAD).unwrap();
    let mid = raw.len() / 2;

    let r1 = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/session/{id}/feed"))
                .body(Body::from(raw[..mid].to_vec()))
                .unwrap(),
        )
        .await
        .unwrap();
    let j1 = body_json(r1).await;
    assert_eq!(j1["events"].as_array().unwrap().len(), 0);
    assert_eq!(j1["stats"]["buffered"], mid as u64);

    let r2 = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/session/{id}/feed"))
                .body(Body::from(raw[mid..].to_vec()))
                .unwrap(),
        )
        .await
        .unwrap();
    let j2 = body_json(r2).await;
    assert_eq!(j2["events"][0]["type"], "frame");
    assert_eq!(j2["events"][0]["sequence"], 9);

    let r3 = app
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/session/{id}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(r3.status(), StatusCode::NO_CONTENT);
}

#[tokio::test]
async fn unknown_session_returns_404() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/session/does-not-exist/feed")
                .body(Body::from(vec![0u8; 2]))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn sample_endpoint_binary_and_hex() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let bin = app
        .clone()
        .oneshot(Request::builder().uri("/sample").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(bin.status(), StatusCode::OK);
    let bytes = body_bytes(bin).await;
    assert_eq!(&bytes[..], sample::build(DEFAULT_MAX_PAYLOAD).as_slice());

    let hex = app
        .oneshot(Request::builder().uri("/sample?format=hex").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let j = body_json(hex).await;
    assert!(j["hex"].as_str().unwrap().starts_with("4e4f4953")); // "NOIS"
}

#[tokio::test]
async fn oversized_query_cap_is_clamped() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    // A 5000-byte frame is valid for the wire cap but server default is 4096;
    // query cannot raise it above the server cap.
    let raw = encode_frame(1, &vec![0u8; 5000], 65535).unwrap();
    let resp = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/parse?max_payload=65000")
                .body(Body::from(raw))
                .unwrap(),
        )
        .await
        .unwrap();
    let j = body_json(resp).await;
    assert_eq!(j["max_payload"], DEFAULT_MAX_PAYLOAD as u64);
    assert_eq!(j["stats"]["oversized"], 1);
    assert_eq!(j["stats"]["frames_ok"], 0);
}

#[tokio::test]
async fn encode_rejects_bad_hex_and_oversize() {
    let app = http::app(DEFAULT_MAX_PAYLOAD);
    let bad_hex = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/encode")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"sequence":1,"payload_hex":"zz"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(bad_hex.status(), StatusCode::BAD_REQUEST);

    let big = serde_json::json!({"sequence":1,"payload_hex": hex::encode(vec![0u8; DEFAULT_MAX_PAYLOAD + 1])}).to_string();
    let over = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/encode")
                .header("content-type", "application/json")
                .body(Body::from(big))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(over.status(), StatusCode::BAD_REQUEST);
}

mod hex {
    pub fn encode(b: Vec<u8>) -> String {
        b.iter()
            .map(|x| format!("{x:02x}"))
            .collect::<String>()
    }
}

fn hex_to_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
