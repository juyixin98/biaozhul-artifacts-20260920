//! End-to-end HTTP tests against the real Axum router bound to an ephemeral
//! port. Requests are sent over a raw TCP connection so no extra HTTP-client
//! dependency is needed. These assert concrete response bodies and error
//! categories — not merely that the endpoint responds.

mod common;

use std::sync::Arc;

use common::test_state;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tritmap::batch::LogicalType;
use tritmap::dataset::Dataset;
use tritmap::loader::CsvColumn;
use tritmap::server::{router, AppState};

async fn raw_request(
    addr: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> (u16, serde_json::Value) {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let body = body.unwrap_or("");
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).await.unwrap();

    let text = String::from_utf8_lossy(&raw);
    let status: u16 = text
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap();
    let payload = text.split("\r\n\r\n").nth(1).unwrap_or("");
    let json: serde_json::Value = if payload.trim().is_empty() {
        serde_json::Value::Null
    } else {
        serde_json::from_str(payload.trim()).unwrap_or_else(|e| {
            panic!("invalid JSON body: {e}\nbody: {payload}")
        })
    };
    (status, json)
}

async fn spawn_server() -> (String, Arc<AppState>) {
    let state = test_state();

    // Load the shipped 19-row fixture through the real CSV loader.
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures/users.csv");
    let cols = vec![
        CsvColumn::new("id", LogicalType::Integer),
        CsvColumn::new("age", LogicalType::Integer),
        CsvColumn::new("city", LogicalType::Text),
        CsvColumn::new("vip", LogicalType::Boolean),
    ];
    let batch = tritmap::loader::load_csv(path, &cols).unwrap();
    let ds = Dataset::load("users", batch, 1, &[]).unwrap();
    state.insert_table("users", ds);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let app = router(state.clone());
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    (addr, state)
}

#[tokio::test]
async fn health_and_catalog() {
    let (addr, _) = spawn_server().await;
    let (s, j) = raw_request(&addr, "GET", "/health", None).await;
    assert_eq!(s, 200);
    assert_eq!(j["ok"], true);
    assert_eq!(j["data"]["status"], "up");

    let (s, j) = raw_request(&addr, "GET", "/tables", None).await;
    assert_eq!(s, 200);
    assert_eq!(j["data"]["tables"][0], "users");

    let (s, j) = raw_request(&addr, "GET", "/tables/users", None).await;
    assert_eq!(s, 200);
    assert_eq!(j["data"]["rows"], 19);
    assert_eq!(j["data"]["data_version"], 1);
    assert_eq!(j["data"]["indexed_columns"], serde_json::json!(["age", "city", "id", "vip"]));
}

#[tokio::test]
async fn query_reports_three_valued_counts_and_rows() {
    let (addr, _) = spawn_server().await;
    let body = r#"{"expr":"age >= 18 AND city IS NOT NULL"}"#;
    let (s, j) = raw_request(&addr, "POST", "/tables/users/query", Some(body)).await;
    assert_eq!(s, 200);
    assert_eq!(j["ok"], true);
    let data = &j["data"];
    assert_eq!(data["row_space"], 19);
    assert_eq!(data["live_rows"], 19);
    // NULL-bearing rows are UNKNOWN, not silently excluded from accounting.
    assert!(data["counts"]["unknown_rows"].as_i64().unwrap() >= 1);
    assert_eq!(
        data["counts"]["true_rows"].as_i64().unwrap()
            + data["counts"]["false_rows"].as_i64().unwrap()
            + data["counts"]["unknown_rows"].as_i64().unwrap(),
        19
    );
    // Verdicts use the exact TRUE/FALSE/UNKNOWN vocabulary.
    let verdicts: Vec<&str> = j["data"]["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["verdict"].as_str().unwrap())
        .collect();
    assert!(verdicts.contains(&"TRUE"));
    assert!(verdicts.contains(&"UNKNOWN"));
}

#[tokio::test]
async fn parse_and_type_errors_are_400_with_categories_not_success() {
    let (addr, _) = spawn_server().await;
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/query",
        Some(r#"{"expr":"age >="}"#),
    )
    .await;
    assert_eq!(s, 400);
    assert_eq!(j["ok"], false);
    assert_eq!(j["error_code"], "QUERY_PARSE_ERROR");

    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/query",
        Some(r#"{"expr":"age = 'NY'"}"#),
    )
    .await;
    assert_eq!(s, 400);
    assert_eq!(j["error_code"], "TYPE_MISMATCH");

    let (s, j) = raw_request(&addr, "GET", "/tables/nope", None).await;
    assert_eq!(s, 404);
    assert_eq!(j["error_code"], "UNKNOWN_TABLE");
}

#[tokio::test]
async fn deletion_removes_rows_and_conflicts_are_409() {
    let (addr, _) = spawn_server().await;

    // Delete row 0 at v2, then query at current version: 18 live rows.
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/rows/0/delete",
        Some(r#"{"version":2}"#),
    )
    .await;
    assert_eq!(s, 200, "{j}");
    assert_eq!(j["ok"], true);

    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/query",
        Some(r#"{"expr":"id IS NOT NULL"}"#),
    )
    .await;
    assert_eq!(s, 200);
    assert_eq!(j["data"]["live_rows"], 18);
    assert_eq!(j["data"]["counts"]["deleted_rows"], 1);
    assert_eq!(j["data"]["version"], 2);

    // Re-deleting the same row at a newer version conflicts (409).
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/rows/0/delete",
        Some(r#"{"version":3}"#),
    )
    .await;
    assert_eq!(s, 409);
    assert_eq!(j["error_code"], "ROW_ALREADY_DELETED");

    // Out-of-range row id -> 404 ROW_NOT_FOUND.
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/rows/19/delete",
        Some(r#"{"version":4}"#),
    )
    .await;
    assert_eq!(s, 404);
    assert_eq!(j["error_code"], "ROW_NOT_FOUND");
}

#[tokio::test]
async fn historical_version_query_keeps_deleted_rows() {
    let (addr, _) = spawn_server().await;
    let _ = raw_request(
        &addr,
        "POST",
        "/tables/users/rows/1/delete",
        Some(r#"{"version":2}"#),
    )
    .await;
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/tables/users/query",
        Some(r#"{"expr":"city = 'LA'","version":1}"#),
    )
    .await;
    assert_eq!(s, 200);
    // Row 1 (city LA) exists at v1.
    assert_eq!(j["data"]["version"], 1);
    let selected: Vec<i64> = j["data"]["selected_rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_i64().unwrap())
        .collect();
    assert!(selected.contains(&1), "row 1 live at v1: {selected:?}");
}

#[tokio::test]
async fn verify_endpoint_runs_real_differential_checks() {
    let (addr, _) = spawn_server().await;
    let (s, j) = raw_request(
        &addr,
        "POST",
        "/verify?iterations=15&rows=67&null_density=40",
        Some(""),
    )
    .await;
    assert_eq!(s, 200, "{j}");
    assert_eq!(j["ok"], true);
    assert_eq!(j["data"]["all_passed"], true);
    assert!(j["data"]["cases"].as_i64().unwrap() >= 14);
    // Reports carry correlation identity and pinned versions.
    let first = &j["data"]["reports"][0];
    assert!(first["run_id"].as_str().unwrap().starts_with("run-"));
    assert_eq!(first["data_version"], 1);
    assert!(first["passed"].as_bool().unwrap());
}
