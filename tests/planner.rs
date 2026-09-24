//! 引擎端到端测试，覆盖验收点：
//! 1. 冷构建 = 全量；
//! 2. 改叶输入 -> 只重建受影响链，菱形共享节点只执行一次；
//! 3. 只改 mtime（touch）-> 不重建；
//! 4. 改构建规则 / 工具版本 / 声明环境 -> 缓存键变化并向下游传播；
//! 5. 无关文件变化 -> 零动作；
//! 6. 环 -> 422 风格错误且可定位；
//! 7. 增量构建结果与全量构建逐节点一致（内容寻址模拟器性质测试）。

use std::collections::{BTreeMap, BTreeSet};

use incr_build_planner::engine::{
    self, build_cache_key, FileObservation, PlanError, PlanRequest, Reason, Snapshot,
};
use incr_build_planner::graph::{Edge, Graph, Node, NodeKind, Rule};

/* ----------------------------- fixtures ----------------------------- */

fn f(id: &str) -> Node {
    Node {
        id: id.into(),
        kind: NodeKind::File,
        rule: None,
    }
}

fn b(id: &str, command: &str) -> Node {
    Node {
        id: id.into(),
        kind: NodeKind::Build,
        rule: Some(Rule {
            command: command.into(),
            tool: "cc".into(),
            tool_version: "13.2.0".into(),
            env: BTreeMap::from([("CFLAGS".to_string(), "-O2".to_string())]),
        }),
    }
}

fn e(from: &str, to: &str) -> Edge {
    Edge {
        from: from.into(),
        to: to.into(),
    }
}

fn obs(hash: &str, mtime: i64) -> FileObservation {
    FileObservation {
        content_hash: hash.into(),
        mtime_ms: mtime,
    }
}

/// 菱形 DAG：
///
/// ```text
/// left.in   right.in
///   \         /
///  compile_l  compile_r
///      \       /
///       link -> app.bin
/// ```
///
/// `app.bin` 是文件产物；两个 compile 汇入一个 link，验证去重。
fn diamond_graph() -> Graph {
    Graph {
        nodes: vec![
            f("left.in"),
            f("right.in"),
            b("compile_l", "cc -c left.in -o left.o"),
            b("compile_r", "cc -c right.in -o right.o"),
            b("link", "cc left.o right.o -o app.bin"),
            f("app.bin"),
        ],
        edges: vec![
            e("left.in", "compile_l"),
            e("right.in", "compile_r"),
            e("compile_l", "link"),
            e("compile_r", "link"),
            e("link", "app.bin"),
        ],
    }
}

fn full_keys(graph: &Graph) -> BTreeMap<String, String> {
    graph
        .nodes
        .iter()
        .filter(|n| n.kind == NodeKind::Build)
        .map(|n| (n.id.clone(), build_cache_key(n.rule.as_ref().unwrap())))
        .collect()
}

fn base_files() -> BTreeMap<String, FileObservation> {
    BTreeMap::from([
        ("left.in".into(), obs("h-left-v1", 1000)),
        ("right.in".into(), obs("h-right-v1", 1000)),
        ("app.bin".into(), obs("h-app-v1", 1000)),
    ])
}

fn snapshot(graph: &Graph, files: BTreeMap<String, FileObservation>) -> Snapshot {
    Snapshot {
        files,
        build_keys: full_keys(graph),
    }
}

fn plan_between(
    graph: Graph,
    previous: Option<Snapshot>,
    files: BTreeMap<String, FileObservation>,
) -> engine::PlanResponse {
    let current = Snapshot {
        files,
        build_keys: BTreeMap::new(),
    };
    engine::plan(PlanRequest {
        graph,
        previous,
        current,
    })
    .expect("plan ok")
}

/* ----------------------------- 验收点 ----------------------------- */

#[test]
fn cold_build_schedules_everything_in_topo_order() {
    let graph = diamond_graph();
    let resp = plan_between(graph, None, base_files());
    assert!(resp.cold_build);
    assert_eq!(
        resp.steps,
        vec!["compile_l", "compile_r", "link"],
        "cold build = full order"
    );
    assert_eq!(resp.steps, resp.full_order);
    assert_eq!(resp.stats.scheduled_builds, 3);
    assert!(resp
        .builds
        .values()
        .all(|x| matches!(x.reason, Some(Reason::CacheMiss))));
}

#[test]
fn modifying_a_leaf_rebuilds_only_its_chain_and_link_runs_once() {
    let graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    let mut cur = base_files();
    cur.insert("left.in".into(), obs("h-left-v2", 2000));

    let resp = plan_between(graph, previous, cur);
    assert!(!resp.cold_build);
    assert_eq!(
        resp.steps,
        vec!["compile_l", "link"],
        "right 链不动；link 即使有两条路径也只出现一次"
    );
    assert_eq!(resp.stats.scheduled_builds, 2);
    assert_eq!(resp.content_changed_files, vec!["left.in"]);
    assert_eq!(
        resp.affected_nodes,
        vec!["app.bin", "compile_l", "left.in", "link"]
    );

    // compile_r 必须命中缓存
    let cr = &resp.builds["compile_r"];
    assert!(cr.reason.is_none(), "compile_r up-to-date");
    // link 的触发者包含 compile_l（仅它实际变化）
    match &resp.builds["link"].reason {
        Some(Reason::InputChanged { triggered_by }) => {
            assert_eq!(triggered_by, &vec!["compile_l".to_string()]);
        }
        other => panic!("unexpected reason: {other:?}"),
    }
    // 产物标记为 Rebuilt
    assert_eq!(
        format!("{:?}", resp.node_states["app.bin"].file_status),
        "Some(Rebuilt)"
    );
}

#[test]
fn both_leaves_change_but_link_still_runs_once_diamond() {
    let graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    let mut cur = base_files();
    cur.insert("left.in".into(), obs("h-left-v2", 2000));
    cur.insert("right.in".into(), obs("h-right-v2", 2000));

    let resp = plan_between(graph, previous, cur);
    assert_eq!(resp.steps, vec!["compile_l", "compile_r", "link"]);
    // 关键验收：link 在 steps 中恰好出现一次
    assert_eq!(
        resp.steps.iter().filter(|s| *s == "link").count(),
        1,
        "diamond join executes once"
    );
    match &resp.builds["link"].reason {
        Some(Reason::InputChanged { triggered_by }) => {
            assert_eq!(triggered_by.len(), 2, "两个直接上游都触发它");
            assert!(triggered_by.contains(&"compile_l".to_string()));
            assert!(triggered_by.contains(&"compile_r".to_string()));
        }
        other => panic!("unexpected: {other:?}"),
    }
}

#[test]
fn touch_only_mtime_change_schedules_nothing() {
    let graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    let mut cur = base_files();
    // 哈希完全不变，只把 mtime 往后拨（模拟 touch / rsync 保留内容但刷新时间）。
    cur.insert("left.in".into(), obs("h-left-v1", 9999));
    cur.insert("right.in".into(), obs("h-right-v1", 9999));

    let resp = plan_between(graph, previous, cur);
    assert!(resp.steps.is_empty(), "仅时间戳变化不得触发构建");
    assert_eq!(resp.timestamp_only_files, vec!["left.in", "right.in"]);
    assert_eq!(resp.stats.timestamp_only_files, 2);
    assert_eq!(resp.stats.scheduled_builds, 0);
    assert!(resp.node_states.values().all(|s| !s.scheduled));
}

#[test]
fn unrelated_file_change_is_ignored() {
    let graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    let mut cur = base_files();
    cur.insert("notes.txt".into(), obs("h-notes-v2", 2000)); // 图里没有它
    cur.insert("/tmp/scratch.log".into(), obs("h-log-v2", 2000));

    let resp = plan_between(graph, previous, cur);
    assert!(resp.steps.is_empty());
    assert_eq!(resp.unrelated_files, vec!["/tmp/scratch.log", "notes.txt"]);
}

#[test]
fn changing_rule_command_invalidates_cache_and_propagates() {
    let mut graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    // 改 link 的命令（规则变化）
    graph.nodes.iter_mut().for_each(|n| {
        if n.id == "link" {
            n.rule.as_mut().unwrap().command = "cc --gc-sections left.o right.o -o app.bin".into();
        }
    });

    let resp = plan_between(graph, previous, base_files());
    assert_eq!(resp.steps, vec!["link"], "只有 link 自身需要重跑");
    assert_eq!(resp.stats.changed_rules, 1);
    match &resp.builds["link"].reason {
        Some(Reason::RuleChanged {
            previous_cache_key,
            current_cache_key,
        }) => {
            assert_ne!(previous_cache_key, current_cache_key);
        }
        other => panic!("unexpected: {other:?}"),
    }
    // app.bin 受牵连
    assert!(resp.affected_nodes.contains(&"app.bin".to_string()));
}

#[test]
fn tool_version_and_declared_env_are_part_of_cache_key() {
    let mut g = diamond_graph();
    let k0 = {
        let rule = g
            .nodes
            .iter()
            .find(|n| n.id == "compile_l")
            .unwrap()
            .rule
            .clone()
            .unwrap();
        build_cache_key(&rule)
    };

    // 只升级工具版本
    g.nodes.iter_mut().for_each(|n| {
        if n.id == "compile_l" {
            n.rule.as_mut().unwrap().tool_version = "14.0.0".into();
        }
    });
    let k1 = build_cache_key(
        g.nodes
            .iter()
            .find(|n| n.id == "compile_l")
            .unwrap()
            .rule
            .as_ref()
            .unwrap(),
    );
    assert_ne!(k0, k1, "工具版本变化必须改变缓存键");

    // 再只改声明环境
    g.nodes.iter_mut().for_each(|n| {
        if n.id == "compile_l" {
            let r = n.rule.as_mut().unwrap();
            r.tool_version = "13.2.0".into();
            r.env.insert("CFLAGS".into(), "-O3".into());
        }
    });
    let k2 = build_cache_key(
        g.nodes
            .iter()
            .find(|n| n.id == "compile_l")
            .unwrap()
            .rule
            .as_ref()
            .unwrap(),
    );
    assert_ne!(k0, k2, "声明环境变化必须改变缓存键");

    // 同样的声明，env 提供顺序不同 -> 键相同（稳定性）
    let mut rule_a = g
        .nodes
        .iter()
        .find(|n| n.id == "compile_l")
        .unwrap()
        .rule
        .clone()
        .unwrap();
    rule_a.env = BTreeMap::from([
        ("A".to_string(), "1".to_string()),
        ("B".to_string(), "2".to_string()),
    ]);
    let mut rule_b = rule_a.clone();
    rule_b.env = BTreeMap::from([
        ("B".to_string(), "2".to_string()),
        ("A".to_string(), "1".to_string()),
    ]);
    assert_eq!(build_cache_key(&rule_a), build_cache_key(&rule_b));
}

#[test]
fn tool_version_change_is_reported_as_rule_change_in_plan() {
    let graph0 = diamond_graph();
    let previous = Some(snapshot(&graph0, base_files()));

    let mut graph1 = diamond_graph();
    graph1.nodes.iter_mut().for_each(|n| {
        if n.id == "compile_r" {
            n.rule.as_mut().unwrap().tool_version = "14.0.0".into();
        }
    });
    let resp = plan_between(graph1, previous, base_files());
    assert_eq!(resp.steps, vec!["compile_r", "link"]);
    assert_eq!(resp.stats.changed_rules, 1);
}

#[test]
fn deleting_an_input_rebuilds_dependents() {
    let graph = diamond_graph();
    let previous = Some(snapshot(&graph, base_files()));

    let mut cur = base_files();
    cur.remove("left.in");

    let resp = plan_between(graph, previous, cur);
    assert_eq!(resp.steps, vec!["compile_l", "link"]);
    assert_eq!(resp.content_changed_files, vec!["left.in"]);
}

#[test]
fn cycle_is_rejected_and_located() {
    let graph = Graph {
        nodes: vec![b("a", "x"), b("b", "x"), b("c", "x"), f("leaf")],
        edges: vec![e("a", "b"), e("b", "c"), e("c", "a"), e("leaf", "a")],
    };
    let PlanError { cycles, .. } = engine::plan(PlanRequest {
        graph,
        previous: None,
        current: Snapshot::default(),
    })
    .unwrap_err();
    assert_eq!(cycles.len(), 1);
    let cy = &cycles[0];
    assert_eq!(cy.nodes.len(), 3);
    let on: BTreeSet<&str> = cy.nodes.iter().map(String::as_str).collect();
    assert!(on.contains("a") && on.contains("b") && on.contains("c"));
    assert!(!on.contains("leaf"));
    assert_eq!(cy.edges.len(), 3);
}

#[test]
fn incremental_plan_converges_after_applying_changes() {
    // 以第一次计划返回的缓存键“执行”后形成新快照；第二次规划应当零动作。
    let graph = diamond_graph();
    let first = plan_between(graph.clone(), None, base_files());
    assert_eq!(first.stats.scheduled_builds, 3);

    let executed_keys: BTreeMap<String, String> = first
        .builds
        .iter()
        .map(|(k, v)| (k.clone(), v.cache_key.clone()))
        .collect();
    let after_build = Snapshot {
        files: base_files(),
        build_keys: executed_keys,
    };
    let second = plan_between(graph, Some(after_build), base_files());
    assert!(second.steps.is_empty(), "构建完成后应完全命中缓存");
    assert!(!second.cold_build);
}

/* ---------------- 性质测试：增量结果 == 全量结果 ----------------
 *
 * 用一个确定性的内容寻址“迷你构建系统”模拟执行：
 * - 每个 build 的产物内容 = hash(命令, 工具版本, 环境, 各上游产物/输入哈希)；
 * - 全量构建：按拓扑序执行所有 build；
 * - 增量构建：只执行 planner 给出的 steps，其它产物沿用旧值。
 *
 * 若 planner 正确，两种方式得到的所有产物哈希必须逐字节一致。
 */
type Scenario = Box<dyn Fn(&mut BTreeMap<String, FileObservation>, &mut Graph)>;

#[test]
fn incremental_execution_matches_full_execution_after_many_scenarios() {
    let scenarios: Vec<Scenario> = vec![
        // 1. 只改一个叶输入
        Box::new(|files, _| {
            files.insert("left.in".into(), obs("h-left-CHANGED", 5000));
        }),
        // 2. 同时改两个叶输入（菱形汇聚）
        Box::new(|files, _| {
            files.insert("left.in".into(), obs("h-left-CHANGED", 5000));
            files.insert("right.in".into(), obs("h-right-CHANGED", 5000));
        }),
        // 3. 只 touch
        Box::new(|files, _| {
            files.insert("left.in".into(), obs("h-left-v1", 5000));
        }),
        // 4. 改规则命令
        Box::new(|_, g| {
            g.nodes.iter_mut().for_each(|n| {
                if n.id == "compile_l" {
                    n.rule.as_mut().unwrap().command = "cc -O9 -c left.in".into();
                }
            });
        }),
        // 5. 改工具版本
        Box::new(|_, g| {
            g.nodes.iter_mut().for_each(|n| {
                if n.id == "link" {
                    n.rule.as_mut().unwrap().tool_version = "99.0.0".into();
                }
            });
        }),
        // 6. 改声明环境
        Box::new(|_, g| {
            g.nodes.iter_mut().for_each(|n| {
                if n.id == "compile_r" {
                    n.rule
                        .as_mut()
                        .unwrap()
                        .env
                        .insert("CFLAGS".into(), "-O3 -DNDEBUG".into());
                }
            });
        }),
        // 7. 改一个无关文件（不影响图）
        Box::new(|files, _| {
            files.insert("README.md".into(), obs("h-readme", 5000));
        }),
    ];

    for (i, mutate) in scenarios.into_iter().enumerate() {
        let mut graph = diamond_graph();
        let mut files = base_files();
        mutate(&mut files, &mut graph);

        // 上一次成功构建的状态（规则基于原始图；注意场景 4-6 改的是当前图，
        // previous 用原始图的键，正好模拟“规则变了”）。
        let prev_graph = diamond_graph();
        let previous = Some(snapshot(&prev_graph, base_files()));

        let resp = plan_between(graph.clone(), previous, files.clone());

        // steps 必须是 full_order 的合法子序列（拓扑合法性）
        let mut fi = 0usize;
        for s in &resp.steps {
            while fi < resp.full_order.len() && &resp.full_order[fi] != s {
                fi += 1;
            }
            assert!(
                fi < resp.full_order.len(),
                "scenario {i}: step {s} not a valid sub-sequence of full order"
            );
            fi += 1;
        }

        let full = simulate_full(&graph, &source_hashes(&graph, &files));
        let incr =
            simulate_incremental(&diamond_graph(), &graph, &files, &base_files(), &resp.steps);

        assert_eq!(
            full, incr,
            "scenario {i}: incremental outputs differ from full build"
        );
    }
}

/// 取出图中“源文件”（不由任何 build 节点产出的 file 节点）的观测哈希。
/// 产物文件的哈希只能由构建产生，不能从观测注入，否则会污染未重建节点。
fn source_hashes(
    graph: &Graph,
    files: &BTreeMap<String, FileObservation>,
) -> BTreeMap<String, String> {
    let compiled = graph.clone().compile().unwrap();
    let produced: BTreeSet<String> = graph
        .edges
        .iter()
        .filter(|e| compiled.is_build(&e.from) && compiled.is_file(&e.to))
        .map(|e| e.to.clone())
        .collect();
    files
        .iter()
        .filter(|(k, _)| compiled.kinds.contains_key(*k) && !produced.contains(*k))
        .map(|(k, v)| (k.clone(), v.content_hash.clone()))
        .collect()
}

/// 计算一个 build 的确定性产物哈希。
fn artifact_hash(graph: &Graph, id: &str, inputs: &BTreeMap<String, String>) -> String {
    let node = graph.nodes.iter().find(|n| n.id == id).unwrap();
    let rule = node.rule.as_ref().unwrap();
    let mut combined = format!(
        "{}|{}|{}|{}",
        id, rule.command, rule.tool, rule.tool_version
    );
    for (k, v) in &rule.env {
        combined.push_str(&format!("|{k}={v}"));
    }
    // 上游按边顺序取已有哈希
    let preds: Vec<&Edge> = graph.edges.iter().filter(|e| e.to == id).collect();
    for e in &preds {
        let h = inputs
            .get(&e.from)
            .cloned()
            .unwrap_or_else(|| "MISSING".to_string());
        combined.push_str(&format!("|{}={}", e.from, h));
    }
    let digest = fnv1a_64(combined.as_bytes());
    format!("art:{digest}")
}

/// 测试专用的确定性 64 位 FNV-1a 哈希：性质测试只要求随输入确定变化，
/// 不要求抗碰撞。
fn fnv1a_64(bytes: &[u8]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        hash ^= *b as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{hash:016x}")
}

/// 全量执行：拓扑序跑所有 build，返回所有节点（源文件 + 产物）哈希。
/// `sources` 只含源文件哈希（产物由构建产生）。
fn simulate_full(graph: &Graph, sources: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    let compiled = graph.clone().compile().unwrap();
    let (topo, _) = compiled.topo_sort();
    let mut hashes: BTreeMap<String, String> = sources.clone();
    for id in topo {
        if compiled.is_build(&id) {
            let h = artifact_hash(graph, &id, &hashes);
            // build 的每个文件产物都拿到相同的确定性产物哈希（本模型只看存在性）。
            for out in graph.edges.iter().filter(|e| e.from == id) {
                if compiled.is_file(&out.to) {
                    hashes.insert(out.to.clone(), h.clone());
                }
            }
            hashes.insert(id.clone(), h);
        }
    }
    hashes
}

/// 增量执行：只跑 steps；旧产物来自上一张图的全量结果。
fn simulate_incremental(
    prev_graph: &Graph,
    cur_graph: &Graph,
    cur_files: &BTreeMap<String, FileObservation>,
    prev_files: &BTreeMap<String, FileObservation>,
    steps: &[String],
) -> BTreeMap<String, String> {
    // 起点 = 上一次全量构建的产物（产物哈希来自构建，不来自观测）。
    let baseline = simulate_full(prev_graph, &source_hashes(prev_graph, prev_files));
    let compiled = cur_graph.clone().compile().unwrap();
    let mut hashes = baseline;
    // 用当前源文件哈希覆盖；产物哈希保持构建结果。
    for (k, v) in source_hashes(cur_graph, cur_files) {
        hashes.insert(k, v);
    }
    // steps 已按拓扑序排列；只重算这些 build 的产物。
    for id in steps {
        let h = artifact_hash(cur_graph, id, &hashes);
        for out in cur_graph.edges.iter().filter(|e| &e.from == id) {
            if compiled.is_file(&out.to) {
                hashes.insert(out.to.clone(), h.clone());
            }
        }
        hashes.insert(id.clone(), h);
    }
    // 只比较当前图中仍然存在的节点
    hashes.retain(|k, _| compiled.kinds.contains_key(k));
    hashes
}
