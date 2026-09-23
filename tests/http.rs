//! End-to-end HTTP tests against the real Axum router + a real SQLite
//! file database, driven in-process through tower `oneshot` (no sockets).

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{json, Value};
use swap_router::{app, Store};
use tower::util::ServiceExt;

struct Harness {
    _dir: tempfile_lite::TempDir,
    db_path: String,
}

mod tempfile_lite {
    use std::path::PathBuf;

    pub struct TempDir(pub PathBuf);
    impl TempDir {
        pub fn new() -> Self {
            let dir = std::env::temp_dir().join(format!(
                "swap-router-itest-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            TempDir(dir)
        }
        pub fn path(&self) -> &std::path::Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}

impl Harness {
    fn new() -> Self {
        let dir = tempfile_lite::TempDir::new();
        let db_path = dir
            .path()
            .join("test.db")
            .to_string_lossy()
            .into_owned();
        Harness { _dir: dir, db_path }
    }

    fn router(&self) -> axum::Router {
        let store = Arc::new(Store::open(&self.db_path).unwrap());
        app(store)
    }
}

async fn call(
    router: axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json");
    let req = match body {
        Some(v) => req.body(Body::from(v.to_string())).unwrap(),
        None => req.body(Body::empty()).unwrap(),
    };
    let resp = router.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    let val: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, val)
}

fn example_snapshot() -> Value {
    json!({
      "assets": [
        { "id": "USDC", "decimals": 6 },
        { "id": "DAI",  "decimals": 18 },
        { "id": "WETH", "decimals": 18 }
      ],
      "pools": [
        { "id": "p1", "token0": "USDC", "token1": "WETH",
          "reserve0": "1000000000000", "reserve1": "300000000000000000000", "fee_bps": 30 },
        { "id": "p2", "token0": "USDC", "token1": "DAI",
          "reserve0": "5000000000000", "reserve1": "5000000000000000000000000", "fee_bps": 1 },
        { "id": "p3", "token0": "DAI", "token1": "WETH",
          "reserve0": "900000000000000000000000", "reserve1": "280000000000000000000", "fee_bps": 5 }
      ]
    })
}

#[tokio::test]
async fn healthz_ok() {
    let h = Harness::new();
    let (status, body) = call(h.router(), "GET", "/healthz", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], json!("ok"));
}

#[tokio::test]
async fn ingest_then_quote_returns_real_integers() {
    let h = Harness::new();
    let (status, body) = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(example_snapshot()),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["snapshot_id"], 1);
    assert!(body["content_hash"].as_str().unwrap().len() == 64);

    let (status, q) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({
            "token_in": "USDC", "token_out": "WETH",
            "amount_in": "1000000000", "slippage_bps": 50
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{q}");
    assert_eq!(q["snapshot_id"], 1);
    let net = q["net_amount_out"].as_str().unwrap();
    assert!(net.parse::<u128>().unwrap() > 0);
    let net_v = net.parse::<u128>().unwrap();
    let min_v = q["min_output"].as_str().unwrap().parse::<u128>().unwrap();
    assert!(min_v < net_v);
    // The cheap 2-hop route (p2 0.01% + p3 0.05%) beats the direct p1
    // (0.30%) — the router compares executed integer outputs, never
    // assumes fewer hops is better.
    let ids: Vec<String> = q["hops"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["pool_id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(ids, vec!["p2", "p3"]);
    // Every hop cites raw reserves and the next hop spends the prior
    // hop's net integer output.
    for h in q["hops"].as_array().unwrap() {
        assert!(h["reserve_in"].is_string());
        assert!(h["reserve_out"].is_string());
    }
    assert_eq!(
        q["hops"][1]["amount_in"],
        q["hops"][0]["net_amount_out"]
    );
}

#[tokio::test]
async fn quote_without_any_snapshot_is_400() {
    let h = Harness::new();
    let (status, body) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({"token_in":"A","token_out":"B","amount_in":"1"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "BAD_REQUEST");
}

#[tokio::test]
async fn unknown_snapshot_id_is_404() {
    let h = Harness::new();
    let _ = call(h.router(), "POST", "/snapshots", Some(example_snapshot())).await;
    let (status, body) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({
            "snapshot_id": 4242, "token_in": "USDC", "token_out": "WETH",
            "amount_in": "1000000000"
        })),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"]["code"], "SNAPSHOT_NOT_FOUND");
}

#[tokio::test]
async fn unknown_precision_rejected_at_ingestion() {
    let h = Harness::new();
    let (status, body) = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [{"id":"A","decimals":18}],
            "pools": [{"id":"p","token0":"A","token1":"GHOST",
                       "reserve0":"1000","reserve1":"1000","fee_bps":30}]
        })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["message"].as_str().unwrap().contains("precision is unknown"));
}

#[tokio::test]
async fn zero_reserve_rejected_at_ingestion() {
    let h = Harness::new();
    let (status, _) = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [{"id":"A","decimals":18},{"id":"B","decimals":6}],
            "pools": [{"id":"p","token0":"A","token1":"B",
                       "reserve0":"0","reserve1":"1000","fee_bps":30}]
        })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn malformed_json_is_400_not_500() {
    let h = Harness::new();
    let router = h.router();
    let req = Request::builder()
        .method("POST")
        .uri("/snapshots")
        .header("content-type", "application/json")
        .body(Body::from("{ not json"))
        .unwrap();
    let resp = router.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn dust_trade_returns_422_no_feasible_path() {
    let h = Harness::new();
    // Small balanced pool: 1 raw unit in floors to zero
    // (floor(9970*1000 / (1000*10000 + 9970)) = 0).
    let (s, _) = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [{"id":"A","decimals":18},{"id":"B","decimals":18}],
            "pools": [{"id":"p1","token0":"A","token1":"B",
                       "reserve0":"1000","reserve1":"1000","fee_bps":30}]
        })),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
    let (status, body) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({"token_in":"A","token_out":"B","amount_in":"1"})),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
    assert_eq!(body["error"]["code"], "NO_FEASIBLE_PATH");
}

#[tokio::test]
async fn hop_limit_structural_nopath_404() {
    let h = Harness::new();
    let _ = call(h.router(), "POST", "/snapshots", Some(example_snapshot())).await;
    // USDC->DAI->WETH exists at 2 hops; here use a chain that needs 3.
    let _ = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [
                {"id":"A","decimals":18},{"id":"B","decimals":18},
                {"id":"C","decimals":18},{"id":"D","decimals":18}
            ],
            "pools": [
                {"id":"ab","token0":"A","token1":"B","reserve0":"1000","reserve1":"1000","fee_bps":0},
                {"id":"bc","token0":"B","token1":"C","reserve0":"1000","reserve1":"1000","fee_bps":0},
                {"id":"cd","token0":"C","token1":"D","reserve0":"1000","reserve1":"1000","fee_bps":0}
            ]
        })),
    )
    .await;
    let (status, body) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({
            "snapshot_id": 2, "token_in":"A","token_out":"D",
            "amount_in":"10","max_hops": 2
        })),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"]["code"], "NO_PATH");

    // at 3 hops the route exists
    let (status, q) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({
            "snapshot_id": 2, "token_in":"A","token_out":"D",
            "amount_in":"10","max_hops": 3
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{q}");
    assert_eq!(q["hops"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn tie_breaks_on_pool_id_over_http() {
    let h = Harness::new();
    let _ = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [
                {"id":"A","decimals":18},{"id":"X","decimals":18},
                {"id":"Y","decimals":18},{"id":"B","decimals":18}
            ],
            "pools": [
                {"id":"pAX","token0":"A","token1":"X","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
                {"id":"pXB","token0":"X","token1":"B","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
                {"id":"pAY","token0":"A","token1":"Y","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
                {"id":"pYB","token0":"Y","token1":"B","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0}
            ]
        })),
    )
    .await;
    let (status, q) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({"snapshot_id":1,"token_in":"A","token_out":"B",
                    "amount_in":"123456","max_hops":2})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{q}");
    let ids: Vec<String> = q["hops"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["pool_id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(ids, vec!["pAX", "pXB"]);
    assert_eq!(q["feasible_paths"], 2);
}

#[tokio::test]
async fn overflow_amounts_return_422_overflow() {
    let h = Harness::new();
    let max = "340282366920938463463374607431768211454"; // u128::MAX - 1
    let (s, _) = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [{"id":"A","decimals":38},{"id":"B","decimals":38}],
            "pools": [{"id":"p1","token0":"A","token1":"B",
                       "reserve0": max, "reserve1": max, "fee_bps": 30}]
        })),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED);
    let (status, body) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({"token_in":"A","token_out":"B","amount_in": max})),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
    assert_eq!(body["error"]["code"], "OVERFLOW");
}

#[tokio::test]
async fn explicit_cost_is_deducted_each_hop() {
    let h = Harness::new();
    let _ = call(
        h.router(),
        "POST",
        "/snapshots",
        Some(json!({
            "assets": [
                {"id":"A","decimals":18},{"id":"M","decimals":18},{"id":"B","decimals":18}
            ],
            "pools": [
                {"id":"p1","token0":"A","token1":"M","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0},
                {"id":"p2","token0":"M","token1":"B","reserve0":"1000000000000","reserve1":"1000000000000","fee_bps":0}
            ]
        })),
    )
    .await;
    let (status, q) = call(
        h.router(),
        "POST",
        "/quotes",
        Some(json!({
            "snapshot_id":1,"token_in":"A","token_out":"B",
            "amount_in":"1000000","cost_per_hop":"1000","max_hops":3
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{q}");
    let hops = q["hops"].as_array().unwrap();
    assert_eq!(hops.len(), 2);
    for h in hops {
        let gross = h["gross_amount_out"].as_str().unwrap().parse::<u128>().unwrap();
        let net = h["net_amount_out"].as_str().unwrap().parse::<u128>().unwrap();
        assert_eq!(net + 1000, gross);
    }
    assert_eq!(
        hops[1]["amount_in"].as_str(),
        hops[0]["net_amount_out"].as_str()
    );
}
