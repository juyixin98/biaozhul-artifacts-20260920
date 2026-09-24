//! 测试公共构造：菱形依赖图 a(input) -> b,c -> d，以及 HTTP 调用辅助。
#![allow(dead_code)]

use std::collections::BTreeMap;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use incremental_build_planner::api;
use incremental_build_planner::model::{
    BuildGraph, FileSummary, GraphNode, NodeKind, PlanRequest, Rule, Snapshot, Tool,
};
use serde_json::Value;
use tower::ServiceExt;

pub const LEAF: &str = "a";
pub const LEFT: &str = "b";
pub const RIGHT: &str = "c";
pub const MERGE: &str = "d";

pub fn gcc() -> Tool {
    Tool { name: "gcc".to_string(), version: "13.2.0".to_string() }
}

pub fn rule(command: &str) -> Rule {
    let mut env = BTreeMap::new();
    env.insert("CC".to_string(), "gcc".to_string());
    Rule { command: command.to_string(), tool: Some(gcc()), environment: env }
}

/// 菱形图：a(input) ─┬─ b ─┐
///                  └─ c ─┴─ d
pub fn diamond_graph() -> BuildGraph {
    BuildGraph {
        nodes: vec![
            GraphNode { id: LEAF.into(), kind: NodeKind::Input, depends_on: vec![], rule: None },
            GraphNode {
                id: LEFT.into(),
                kind: NodeKind::Target,
                depends_on: vec![LEAF.into()],
                rule: Some(rule("gcc -c b.c -o b.o")),
            },
            GraphNode {
                id: RIGHT.into(),
                kind: NodeKind::Target,
                depends_on: vec![LEAF.into()],
                rule: Some(rule("gcc -c c.c -o c.o")),
            },
            GraphNode {
                id: MERGE.into(),
                kind: NodeKind::Target,
                depends_on: vec![LEFT.into(), RIGHT.into()],
                rule: Some(rule("ld b.o c.o -o app")),
            },
        ],
    }
}

pub fn files(hash: &str, mtime: i64) -> BTreeMap<String, FileSummary> {
    let mut m = BTreeMap::new();
    m.insert(LEAF.into(), FileSummary { content_hash: hash.into(), mtime: Some(mtime) });
    m
}

pub fn cold_request() -> PlanRequest {
    PlanRequest { graph: diamond_graph(), files: files("hash-a-v1", 1000), baseline: None }
}

pub fn incremental_request(baseline: Snapshot) -> PlanRequest {
    PlanRequest { graph: diamond_graph(), files: files("hash-a-v1", 1000), baseline: Some(baseline) }
}

pub async fn post_plan(req: &PlanRequest) -> (StatusCode, Value) {
    let app = api::router();
    let http_req = Request::builder()
        .method("POST")
        .uri("/plan")
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(req).unwrap()))
        .unwrap();
    let resp = app.oneshot(http_req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let val: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, val)
}

pub async fn post_plan_raw(body: &str) -> (StatusCode, Value) {
    let app = api::router();
    let http_req = Request::builder()
        .method("POST")
        .uri("/plan")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = app.oneshot(http_req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let val: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, val)
}

pub async fn get_health() -> StatusCode {
    let app = api::router();
    let http_req =
        Request::builder().method("GET").uri("/healthz").body(Body::empty()).unwrap();
    let resp = app.oneshot(http_req).await.unwrap();
    resp.status()
}

/// 从响应里取 next_snapshot 并反序列化。
pub fn snapshot_of(resp: &Value) -> Snapshot {
    serde_json::from_value(resp["next_snapshot"].clone()).unwrap()
}

pub fn step_nodes(v: &Value) -> Vec<String> {
    v["incremental"]["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["node"].as_str().unwrap().to_string())
        .collect()
}
