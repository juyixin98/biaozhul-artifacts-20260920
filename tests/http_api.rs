//! End-to-end HTTP tests against the real Axum router backed by SQLite.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use p016_quote_router::amount::Amount;
use p016_quote_router::api;
use p016_quote_router::db::Store;
use p016_quote_router::model::Snapshot;
use serde_json::{json, Value};
use tower::ServiceExt;

fn app() -> axum::Router {
    let store = Arc::new(Store::open(":memory:").unwrap());
    api::router(store)
}

async fn send(app: &axum::Router, req: Request<Body>) -> (StatusCode, Value) {
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

fn example_snapshot() -> Value {
    json!({
        "assets": [
            {"id": "USDC", "decimals": 6},
            {"id": "WETH", "decimals": 18},
            {"id": "DAI",  "decimals": 18}
        ],
        "pools": [
            {
                "id": "weth_usdc",
                "token0": "WETH", "token1": "USDC",
                "reserve0": "1000000000000000000000",
                "reserve1": "3000000000000",
                "fee_bps": 30,
                "cost_token0_out": "0",
                "cost_token1_out": "0"
            },
            {
                "id": "weth_dai",
                "token0": "WETH", "token1": "DAI",
                "reserve0": "500000000000000000000",
                "reserve1": "1500000000000000000000000",
                "fee_bps": 30,
                "cost_token0_out": "0",
                "cost_token1_out": "0"
            },
            {
                "id": "dai_usdc",
                "token0": "DAI", "token1": "USDC",
                "reserve0": "1000000000000000000000000",
                "reserve1": "999000000000",
                "fee_bps": 5,
                "cost_token0_out": "0",
                "cost_token1_out": "1000"
            }
        ]
    })
}

async fn upload(app: &axum::Router, body: Value) -> (StatusCode, String, bool) {
    let req = Request::builder()
        .method("POST")
        .uri("/v1/snapshots")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, value) = send(app, req).await;
    let id = value["snapshot_id"].as_str().unwrap_or("").to_string();
    let created = value["created"].as_bool().unwrap_or(false);
    (status, id, created)
}

#[tokio::test]
async fn healthz_ok() {
    let app = app();
    let req = Request::builder().uri("/healthz").body(Body::empty()).unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(value["status"], "ok");
}

#[tokio::test]
async fn upload_idempotent_and_listable() {
    let app = app();
    let (s1, id1, created1) = upload(&app, example_snapshot()).await;
    assert_eq!(s1, StatusCode::CREATED);
    assert_eq!(id1.len(), 64);
    assert!(created1);

    let (s2, id2, created2) = upload(&app, example_snapshot()).await;
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(id1, id2);
    assert!(!created2);

    let req = Request::builder()
        .uri("/v1/snapshots")
        .body(Body::empty())
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(value["snapshot_ids"][0].as_str().unwrap(), id1);

    let req = Request::builder()
        .uri(format!("/v1/snapshots/{id1}"))
        .body(Body::empty())
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::OK);
    let stored: Snapshot = serde_json::from_value(value).unwrap();
    assert_eq!(stored.assets.len(), 3);
}

#[tokio::test]
async fn snapshot_id_is_real_sha256_of_canonical_json() {
    let app = app();
    let body = example_snapshot();
    let (_, id, _) = upload(&app, body.clone()).await;

    // Compute independently over order-normalized canonical JSON.
    let snap: Snapshot = serde_json::from_value(body).unwrap();
    assert_eq!(id, snap.id());
}

#[tokio::test]
async fn quote_returns_snapshot_id_hops_and_min_output() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;

    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({
                "asset_in": "WETH",
                "asset_out": "USDC",
                "amount_in": "1000000000000000000",
                "slippage_bps": 50
            })
            .to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::OK, "{value}");
    assert_eq!(value["snapshot_id"], id);
    assert_eq!(value["asset_in"], "WETH");
    assert_eq!(value["asset_out"], "USDC");
    assert_eq!(value["asset_in_decimals"], 18);
    assert_eq!(value["asset_out_decimals"], 6);
    assert!(value["amount_out"].is_string());
    assert!(value["min_amount_out"].is_string());
    assert!(value["hops"].is_array());
    for hop in value["hops"].as_array().unwrap() {
        assert!(hop["basis"].as_str().unwrap().contains("floor"));
        assert!(hop["reserve_in"].is_string());
        assert!(hop["reserve_out"].is_string());
    }
    // min_amount_out = floor(amount_out * 9950 / 10000)
    let out: u128 = value["amount_out"].as_str().unwrap().parse().unwrap();
    let min_out: u128 = value["min_amount_out"].as_str().unwrap().parse().unwrap();
    assert_eq!(min_out, out * 9950 / 10000);
}

#[tokio::test]
async fn quote_defaults_slippage_to_zero() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "USDC", "asset_out": "WETH", "amount_in": "1000000000"})
                .to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::OK, "{value}");
    assert_eq!(value["slippage_bps"], 0);
    assert_eq!(value["min_amount_out"], value["amount_out"]);
}

#[tokio::test]
async fn unknown_asset_in_quote_is_refused() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "MONOPOLY", "asset_out": "USDC", "amount_in": "1"})
                .to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(value["error"]["code"], "ASSET_PRECISION_UNKNOWN");
}

#[tokio::test]
async fn missing_snapshot_404() {
    let app = app();
    let req = Request::builder()
        .method("POST")
        .uri("/v1/snapshots/0000000000000000000000000000000000000000000000000000000000000000/quote")
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "A", "asset_out": "B", "amount_in": "1"}).to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(value["error"]["code"], "SNAPSHOT_NOT_FOUND");
}

#[tokio::test]
async fn zero_reserve_pool_rejected_at_upload() {
    let app = app();
    let mut bad = example_snapshot();
    bad["pools"][0]["reserve1"] = json!("0");
    let req = Request::builder()
        .method("POST")
        .uri("/v1/snapshots")
        .header("content-type", "application/json")
        .body(Body::from(bad.to_string()))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(value["error"]["code"], "INVALID_SNAPSHOT");
    assert!(value["error"]["message"].as_str().unwrap().contains("zero reserve"));
}

#[tokio::test]
async fn missing_precision_rejected_via_validation() {
    let app = app();
    let body = json!({
        "assets": [{"id": "A", "decimals": 18}],
        "pools": [{
            "id": "p", "token0": "A", "token1": "GHOST",
            "reserve0": "1", "reserve1": "1", "fee_bps": 0
        }]
    });
    let req = Request::builder()
        .method("POST")
        .uri("/v1/snapshots")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(value["error"]["code"], "INVALID_SNAPSHOT");
}

#[tokio::test]
async fn no_route_returns_edge_failures() {
    let app = app();
    let body = json!({
        "assets": [
            {"id": "A", "decimals": 18}, {"id": "B", "decimals": 18},
            {"id": "C", "decimals": 18}
        ],
        "pools": [
            {"id": "pAC", "token0": "A", "token1": "C",
             "reserve0": "1000", "reserve1": "1000", "fee_bps": 0,
             "cost_token0_out": "0", "cost_token1_out": "0"},
            {"id": "pCB", "token0": "C", "token1": "B",
             "reserve0": "1000", "reserve1": "1000", "fee_bps": 0,
             "cost_token0_out": "0", "cost_token1_out": "999999"}
        ]
    });
    let (_, id, _) = upload(&app, body).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "A", "asset_out": "B", "amount_in": "500"}).to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(value["error"]["code"], "NO_ROUTE");
    let failures = value["error"]["details"]["edge_failures"].as_array().unwrap();
    assert!(failures.iter().any(|f| f["pool_id"] == "pCB"));
}

#[tokio::test]
async fn json_number_amount_is_rejected() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "USDC", "asset_out": "WETH", "amount_in": 1000}).to_string(),
        ))
        .unwrap();
    let (status, _value) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY); // axum Json rejection
}

#[tokio::test]
async fn bad_slippage_rejected() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"asset_in": "USDC", "asset_out": "WETH", "amount_in": "1", "slippage_bps": 10001})
                .to_string(),
        ))
        .unwrap();
    let (status, value) = send(&app, req).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(value["error"]["code"], "BAD_SLIPPAGE");
}

#[tokio::test]
async fn amount_above_u128_rejected() {
    let app = app();
    let (_, id, _) = upload(&app, example_snapshot()).await;
    let req = Request::builder()
        .method("POST")
        .uri(format!("/v1/snapshots/{id}/quote"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({
                "asset_in": "USDC", "asset_out": "WETH",
                "amount_in": "999999999999999999999999999999999999999"
            })
            .to_string(),
        ))
        .unwrap();
    let (status, _value) = send(&app, req).await;
    // Axum rejects the body while extracting QuoteRequest because the amount
    // string does not fit u128; the response is its 422 rejection.
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[tokio::test]
async fn unknown_fields_are_rejected() {
    let app = app();
    let mut body = example_snapshot();
    body["bogus"] = json!(1);
    let req = Request::builder()
        .method("POST")
        .uri("/v1/snapshots")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let (status, _) = send(&app, req).await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
}

#[test]
fn amount_type_compiles_as_string_only() {
    let a: Amount = serde_json::from_str("\"123\"").unwrap();
    assert_eq!(a.value(), 123);
}
