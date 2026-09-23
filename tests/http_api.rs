//! 端到端 HTTP 接口测试：通过 axum Router（不绑定真实端口）验证 API 行为。

use lsm_kv::{app, Config, Db};
use serde_json::{json, Value};
use std::sync::Arc;
use tempfile::TempDir;
use tower::util::ServiceExt;

use axum::{
    body::Body,
    http::{Request, StatusCode},
};

async fn call(
    router: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let req = Request::builder().method(method).uri(uri);
    let req = match body {
        Some(b) => req
            .header("content-type", "application/json")
            .body(Body::from(b.to_string()))
            .unwrap(),
        None => req.body(Body::empty()).unwrap(),
    };
    let resp = router.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

fn test_db(dir: &TempDir) -> Arc<Db> {
    let cfg = Config::new(dir.path())
        .memtable_entries(2)
        .background_flush(false);
    Db::open(cfg).unwrap()
}

#[tokio::test]
async fn http_put_get_delete_scan_lifecycle() {
    let dir = TempDir::new().unwrap();
    let router = app(test_db(&dir));

    let (s, _) = call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"k","value":"v1"})),
    )
    .await;
    assert_eq!(s, StatusCode::OK);
    let (s, _) = call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"a","value":"1"})),
    )
    .await;
    assert_eq!(s, StatusCode::OK);

    let (s, v) = call(&router, "GET", "/get?key=k", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["found"], json!(true));
    assert_eq!(v["value"], json!("v1"));

    let (s, v) = call(&router, "GET", "/get?key=missing", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["found"], json!(false));
    assert!(v.get("value").is_none());

    let (s, _) = call(&router, "POST", "/delete", Some(json!({"key":"k"}))).await;
    assert_eq!(s, StatusCode::OK);

    let (s, v) = call(&router, "GET", "/get?key=k", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["found"], json!(false));

    let (s, v) = call(&router, "GET", "/scan", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(
        v["items"],
        json!([["a", "1"]]),
        "k 被删除后不应出现在扫描中"
    );
}

#[tokio::test]
async fn http_flush_compact_state_and_persistence() {
    let dir = TempDir::new().unwrap();
    let db = test_db(&dir);
    let router = app(db.clone());

    // 三层覆盖 k：v1 -> v2 -> 删除
    call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"k","value":"v1"})),
    )
    .await;
    call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"a","value":"1"})),
    )
    .await;
    let (s, v) = call(&router, "POST", "/flush", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["segment_ids"], json!([1]));

    call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"k","value":"v2"})),
    )
    .await;
    call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"b","value":"2"})),
    )
    .await;
    call(&router, "POST", "/flush", None).await;

    call(&router, "POST", "/delete", Some(json!({"key":"k"}))).await;
    call(
        &router,
        "POST",
        "/put",
        Some(json!({"key":"c","value":"3"})),
    )
    .await;
    call(&router, "POST", "/flush", None).await;

    let (s, v) = call(&router, "GET", "/state", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["segments"].as_array().unwrap().len(), 3);

    // 故障注入：合并中断
    let (s, v) = call(
        &router,
        "POST",
        "/compact",
        Some(json!({"mode":"full","crash":"after_new_segment_before_manifest"})),
    )
    .await;
    assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(v["error"].as_str().unwrap().contains("crash injection"));

    // 重启进程（重新 open 同一目录）
    drop(db);
    let db2 = {
        let cfg = Config::new(dir.path())
            .memtable_entries(2)
            .background_flush(false);
        Db::open(cfg).unwrap()
    };
    let router2 = app(db2.clone());

    let (s, v) = call(&router2, "GET", "/state", None).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(
        v["segments"].as_array().unwrap().len(),
        3,
        "清单未切换，回到崩溃前状态"
    );

    let (_, v) = call(&router2, "GET", "/get?key=k", None).await;
    assert_eq!(v["found"], json!(false), "墓碑仍遮蔽旧值");

    let (_, v) = call(&router2, "GET", "/scan", None).await;
    assert_eq!(
        v["items"],
        json!([["a", "1"], ["b", "2"], ["c", "3"]]),
        "无重复、无旧值复活"
    );

    // 完成正式合并：墓碑被确认安全丢弃
    let (s, v) = call(&router2, "POST", "/compact", Some(json!({"mode":"full"}))).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(v["report"]["dropped_tombstones"], json!(["k"]));
    assert_eq!(v["report"]["new_segment_id"], json!(4));
}

#[tokio::test]
async fn http_bad_requests_are_reported() {
    let dir = TempDir::new().unwrap();
    let router = app(test_db(&dir));

    // 未知故障注入点 -> 400
    let (s, v) = call(
        &router,
        "POST",
        "/compact",
        Some(json!({"mode":"full","crash":"nope"})),
    )
    .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    assert!(v["error"].as_str().unwrap().contains("unknown crash point"));

    // range 模式缺参数 -> 400
    let (s, _) = call(&router, "POST", "/compact", Some(json!({"mode": "range"}))).await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // 未知 mode -> 500（anyhow 错误统一 500）
    let (s, v) = call(&router, "POST", "/compact", Some(json!({"mode":"banana"}))).await;
    assert_eq!(s, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(v["error"].as_str().unwrap().contains("unknown mode"));
}
