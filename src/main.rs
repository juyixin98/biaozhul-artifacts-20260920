//! Axum HTTP layer for the license propagation service.
//!
//! Endpoints:
//! - `GET  /health`
//! - `POST /evaluate` — full policy evaluation
//! - `POST /parse`    — SPDX expression parse/DNF preview
//!
//! Run: `cargo run` (default port 8080, override with `PORT`).

use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

use license_propagation::{describe_expression, evaluate, EvaluateRequest};

async fn health() -> Json<Value> {
    Json(json!({
        "status": "ok",
        "service": "license-propagation",
        "note": "rule engine only, not legal advice",
    }))
}

async fn evaluate_handler(
    Json(req): Json<EvaluateRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    match evaluate(&req) {
        Ok(resp) => Ok(Json(serde_json::to_value(resp).unwrap_or_else(|e| {
            json!({"serializationError": e.to_string()})
        }))),
        Err(msg) => Err(bad_request(msg)),
    }
}

async fn parse_handler(
    Json(body): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let input = body
        .get("expression")
        .and_then(|v| v.as_str())
        .ok_or_else(|| bad_request("missing string field \"expression\"".to_string()))?;
    describe_expression(input)
        .map(Json)
        .map_err(bad_request)
}

fn bad_request(msg: String) -> (StatusCode, Json<Value>) {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({ "error": msg })),
    )
}

fn router() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/evaluate", post(evaluate_handler))
        .route("/parse", post(parse_handler))
}

#[tokio::main]
async fn main() {
    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".to_string());
    let addr: std::net::SocketAddr = format!("0.0.0.0:{port}")
        .parse()
        .expect("invalid PORT");
    let app = router();
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));
    eprintln!(
        "license-propagation listening on http://{addr} (POST /evaluate, POST /parse, GET /health)"
    );
    axum::serve(listener, app).await.expect("server error");
}

#[cfg(test)]
mod tests {
    use super::router;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    #[tokio::test]
    async fn health_works() {
        let resp = router()
            .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn evaluate_end_to_end() {
        let body = r#"{
          "graph": {
            "root": "app",
            "packages": [
              {"id": "app", "license": "MIT"},
              {"id": "lib", "license": "MIT OR GPL-3.0-only"}
            ],
            "edges": [{"from": "app", "to": "lib", "linking": "static"}]
          }
        }"#;
        let resp = router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/evaluate")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["satisfiable"], true);
        assert_eq!(v["selection"]["lib"]["expression"], "MIT");
    }

    #[tokio::test]
    async fn bad_request_on_invalid_graph() {
        let body = r#"{"graph": {"root": "nope", "packages": [], "edges": []}}"#;
        let resp = router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/evaluate")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
