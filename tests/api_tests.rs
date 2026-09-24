//! 验收测试：叶输入修改、仅时间戳修改、规则/工具/声明环境修改、无关文件、
//! 菱形依赖只执行一次、环可定位，以及增量结果与全量结果逐产物一致。

mod common;

use std::collections::BTreeMap;

use common::*;
use incremental_build_planner::model::{
    BuildGraph, FileSummary, GraphNode, NodeKind, PlanRequest, Rule,
};
use serde_json::json;

// --------------------------------------------------------------------------
// 1. 冷启动 = 全量构建；菱形中合并节点 d 只出现一次
// --------------------------------------------------------------------------
#[tokio::test]
async fn cold_build_runs_all_targets_and_diamond_merge_once() {
    let (status, resp) = post_plan(&cold_request()).await;
    assert_eq!(status, 200);
    assert_eq!(resp["status"], "full_build");

    let steps = step_nodes(&resp);
    assert_eq!(steps, vec!["b", "c", "d"], "must be topological order");
    assert_eq!(resp["incremental"]["step_count"], 3);
    assert_eq!(resp["incremental"]["skipped_count"], 0);
    // 全量对照：同样 3 步
    assert_eq!(resp["full_build"]["step_count"], 3);
    // 合并节点 d 在步骤列表中恰好出现一次（菱形两条传播路径汇合）
    let count = steps.iter().filter(|n| n.as_str() == MERGE).count();
    assert_eq!(count, 1, "diamond merge node must be built exactly once");

    assert_eq!(resp["simulation"]["equivalent"], true);
    assert_eq!(resp["simulation"]["cache_hits_valid"], true);
}

// --------------------------------------------------------------------------
// 2. 修改叶子内容：b、c 都受影响，d 只重建一次；产物与全量构建一致
// --------------------------------------------------------------------------
#[tokio::test]
async fn leaf_content_change_propagates_and_matches_full_build() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut req = incremental_request(snap);
    req.files = files("hash-a-v2", 1001); // 内容变了，时间也变了

    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(resp["status"], "incremental");

    // 变更被归类为 content_changed，而不是 timestamp_only
    let kinds: Vec<&str> = resp["changed_inputs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["kind"].as_str().unwrap())
        .collect();
    assert_eq!(kinds, vec!["content_changed"]);
    assert!(resp["timestamp_only_inputs"].as_array().unwrap().is_empty());

    let steps = step_nodes(&resp);
    assert_eq!(steps, vec!["b", "c", "d"]);
    let d_count = steps.iter().filter(|n| n.as_str() == MERGE).count();
    assert_eq!(d_count, 1, "diamond merge must still execute once");

    // 每个步骤的原因：b/c 因上游，d 因上游
    let reason = |node: &str| -> String {
        resp["incremental"]["steps"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["node"] == node)
            .unwrap()["reason"]
            .as_str()
            .unwrap()
            .to_string()
    };
    assert_eq!(reason(LEFT), "dependency_changed");
    assert_eq!(reason(RIGHT), "dependency_changed");
    assert_eq!(reason(MERGE), "dependency_changed");

    // 与全量构建对照：产物逐节点一致
    assert_eq!(resp["simulation"]["equivalent"], true);
    assert_eq!(resp["simulation"]["mismatches"].as_array().unwrap().len(), 0);
    assert_eq!(
        resp["simulation"]["incremental_artifacts"],
        resp["simulation"]["full_artifacts"]
    );
}

// --------------------------------------------------------------------------
// 3. 仅 mtime 变化（内容哈希相同）：零重建
// --------------------------------------------------------------------------
#[tokio::test]
async fn timestamp_only_change_builds_nothing() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut req = incremental_request(snap);
    req.files = files("hash-a-v1", 9999); // 同内容，新 mtime

    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(resp["incremental"]["step_count"], 0);
    assert_eq!(resp["incremental"]["skipped_count"], 3);
    assert_eq!(resp["timestamp_only_inputs"][0], LEAF);
    let kinds: Vec<&str> = resp["changed_inputs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["kind"].as_str().unwrap())
        .collect();
    assert_eq!(kinds, vec!["timestamp_only"]);
    // 跳过的缓存仍被验证为有效
    assert_eq!(resp["simulation"]["cache_hits_valid"], true);
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 4. 无关文件：files 里多出不属于图的条目 -> 忽略且零重建
// --------------------------------------------------------------------------
#[tokio::test]
async fn unrelated_file_is_ignored_and_builds_nothing() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut req = incremental_request(snap);
    let mut f = files("hash-a-v1", 1000);
    f.insert("README.md".into(), FileSummary { content_hash: "xxx".into(), mtime: Some(2) });
    req.files = f;

    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(resp["ignored_files"][0], "README.md");
    assert_eq!(resp["incremental"]["step_count"], 0);
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 5. 构建规则变化（命令）：仅 d 重建；叶子时间戳相同
// --------------------------------------------------------------------------
#[tokio::test]
async fn rule_command_change_rebuilds_only_that_target() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut graph = diamond_graph();
    graph.nodes[3].rule = Some(rule("ld b.o c.o -o app-v2")); // 改 d 的命令

    let req = PlanRequest { graph, files: files("hash-a-v1", 1000), baseline: Some(snap) };
    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(step_nodes(&resp), vec![MERGE]);
    assert_eq!(resp["rule_changes"][0]["node"], MERGE);
    assert_eq!(resp["rule_changes"][0]["details"][0], "command_changed");
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 6. 工具版本变化：缓存键必须覆盖工具版本 -> 重建
// --------------------------------------------------------------------------
#[tokio::test]
async fn tool_version_change_rebuilds() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut graph = diamond_graph();
    let mut r = rule("gcc -c b.c -o b.o");
    r.tool.as_mut().unwrap().version = "14.1.0".into();
    graph.nodes[1].rule = Some(r);

    let req = PlanRequest { graph, files: files("hash-a-v1", 1000), baseline: Some(snap) };
    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(step_nodes(&resp), vec!["b", "d"], "b rebuilt for tool bump, d rebuilt transitively; c cached");
    assert_eq!(resp["rule_changes"][0]["details"][0], "tool_version_changed");
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 7. 声明环境变化（CC 取值改变）：缓存键覆盖环境 -> 重建
// --------------------------------------------------------------------------
#[tokio::test]
async fn declared_environment_change_rebuilds() {
    let (_, first) = post_plan(&cold_request()).await;
    let snap = snapshot_of(&first);

    let mut graph = diamond_graph();
    let mut env = BTreeMap::new();
    env.insert("CC".to_string(), "clang".to_string()); // gcc -> clang
    graph.nodes[2].rule = Some(Rule {
        command: "gcc -c c.c -o c.o".into(),
        tool: Some(gcc()),
        environment: env,
    });

    let req = PlanRequest { graph, files: files("hash-a-v1", 1000), baseline: Some(snap) };
    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 200);
    assert_eq!(step_nodes(&resp), vec!["c", "d"]);
    assert_eq!(resp["rule_changes"][0]["details"][0], "environment_changed");
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 8. 链式传播：更长的链上中间节点变更会传递到底
// --------------------------------------------------------------------------
#[tokio::test]
async fn transitive_change_propagates_through_chain() {
    // 图：x(input) -> m -> n
    let graph = BuildGraph {
        nodes: vec![
            GraphNode { id: "x".into(), kind: NodeKind::Input, depends_on: vec![], rule: None },
            GraphNode {
                id: "m".into(),
                kind: NodeKind::Target,
                depends_on: vec!["x".into()],
                rule: Some(rule("make m")),
            },
            GraphNode {
                id: "n".into(),
                kind: NodeKind::Target,
                depends_on: vec!["m".into()],
                rule: Some(rule("make n")),
            },
        ],
    };
    let mut fs = BTreeMap::new();
    fs.insert("x".into(), FileSummary { content_hash: "h1".into(), mtime: Some(1) });
    let req1 = PlanRequest { graph: graph.clone(), files: fs.clone(), baseline: None };
    let (_, first) = post_plan(&req1).await;
    let snap = snapshot_of(&first);

    fs.insert("x".into(), FileSummary { content_hash: "h2".into(), mtime: Some(2) });
    let req2 = PlanRequest { graph, files: fs, baseline: Some(snap) };
    let (status, resp) = post_plan(&req2).await;
    assert_eq!(status, 200);
    assert_eq!(step_nodes(&resp), vec!["m", "n"]);
    assert_eq!(resp["simulation"]["equivalent"], true);
}

// --------------------------------------------------------------------------
// 9. 环：必须可定位（节点路径 + 边），返回 422
// --------------------------------------------------------------------------
#[tokio::test]
async fn cycle_is_detected_and_located() {
    let graph = BuildGraph {
        nodes: vec![
            GraphNode { id: "x".into(), kind: NodeKind::Input, depends_on: vec![], rule: None },
            GraphNode {
                id: "p".into(),
                kind: NodeKind::Target,
                depends_on: vec!["x".into(), "r".into()],
                rule: Some(rule("p")),
            },
            GraphNode {
                id: "r".into(),
                kind: NodeKind::Target,
                depends_on: vec!["p".into()],
                rule: Some(rule("r")),
            },
        ],
    };
    let mut fs = BTreeMap::new();
    fs.insert("x".into(), FileSummary { content_hash: "h".into(), mtime: None });
    let req = PlanRequest { graph, files: fs, baseline: None };

    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 422);
    assert_eq!(resp["error"], "cycle_detected");
    let nodes: Vec<String> = resp["cycle"]["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    // 环首尾重复
    assert_eq!(*nodes.first().unwrap(), *nodes.last().unwrap());
    assert!(nodes.contains(&"p".to_string()) && nodes.contains(&"r".to_string()));
    let edge_pairs: Vec<(String, String)> = resp["cycle"]["edges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| (e["from"].as_str().unwrap().into(), e["to"].as_str().unwrap().into()))
        .collect();
    assert!(edge_pairs.contains(&("p".into(), "r".into())));
    assert!(edge_pairs.contains(&("r".into(), "p".into())));
}

#[tokio::test]
async fn self_loop_is_reported_as_cycle() {
    let graph = BuildGraph {
        nodes: vec![GraphNode {
            id: "s".into(),
            kind: NodeKind::Target,
            depends_on: vec!["s".into()],
            rule: Some(rule("s")),
        }],
    };
    let req = PlanRequest { graph, files: BTreeMap::new(), baseline: None };
    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 422);
    assert_eq!(resp["error"], "cycle_detected");
}

// --------------------------------------------------------------------------
// 10. 请求错误：未知依赖、缺失文件、目标缺规则、JSON 非法
// --------------------------------------------------------------------------
#[tokio::test]
async fn unknown_dependency_is_400() {
    let graph = BuildGraph {
        nodes: vec![GraphNode {
            id: "t".into(),
            kind: NodeKind::Target,
            depends_on: vec!["ghost".into()],
            rule: Some(rule("t")),
        }],
    };
    let req = PlanRequest { graph, files: BTreeMap::new(), baseline: None };
    let (status, resp) = post_plan(&req).await;
    assert_eq!(status, 400);
    assert_eq!(resp["error"], "invalid_request");
    assert!(resp["errors"][0].as_str().unwrap().contains("unknown node 'ghost'"));
}

#[tokio::test]
async fn missing_file_summary_is_400() {
    let (status, resp) =
        post_plan_raw(&json!({"graph": diamond_graph(), "files": {}}).to_string()).await;
    assert_eq!(status, 400);
    assert!(resp["errors"][0].as_str().unwrap().contains("missing current file summary"));
}

#[tokio::test]
async fn target_without_rule_is_400() {
    let graph = BuildGraph {
        nodes: vec![
            GraphNode { id: "x".into(), kind: NodeKind::Input, depends_on: vec![], rule: None },
            GraphNode {
                id: "t".into(),
                kind: NodeKind::Target,
                depends_on: vec!["x".into()],
                rule: None,
            },
        ],
    };
    let mut fs = BTreeMap::new();
    fs.insert("x".into(), FileSummary { content_hash: "h".into(), mtime: None });
    let req = PlanRequest { graph, files: fs, baseline: None };
    let (status, _) = post_plan(&req).await;
    assert_eq!(status, 400);
}

#[tokio::test]
async fn malformed_json_is_400() {
    let (status, resp) = post_plan_raw("{not json").await;
    assert_eq!(status, 400);
    assert_eq!(resp["error"], "invalid_json");
}

// --------------------------------------------------------------------------
// 11. 健康检查
// --------------------------------------------------------------------------
#[tokio::test]
async fn healthz_ok() {
    assert_eq!(get_health().await, 200);
}

// --------------------------------------------------------------------------
// 12. 内容变化后再增量构建，返回的快照可用于第三次请求且结果稳定（幂等）
// --------------------------------------------------------------------------
#[tokio::test]
async fn repeated_plans_converge_to_noop() {
    let (_, r1) = post_plan(&cold_request()).await;
    let s1 = snapshot_of(&r1);
    let mut req = incremental_request(s1);
    req.files = files("hash-a-v2", 1001);
    let (_, r2) = post_plan(&req).await;
    let s2 = snapshot_of(&r2);

    // 用新快照、完全相同的输入再请求一次 -> 零步
    let req3 = PlanRequest {
        graph: diamond_graph(),
        files: files("hash-a-v2", 1001),
        baseline: Some(s2),
    };
    let (status, r3) = post_plan(&req3).await;
    assert_eq!(status, 200);
    assert_eq!(step_nodes(&r3).len(), 0);
    assert_eq!(r3["simulation"]["equivalent"], true);
}
