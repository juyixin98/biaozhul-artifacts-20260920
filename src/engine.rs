//! 增量构建规划引擎（无状态、纯函数）。
//!
//! 输入：依赖图 + 上一次观测快照 + 当前观测快照。
//! 输出：本次需要执行的构建动作（拓扑序、去重）、每个节点的状态分类、
//!       以及与全量构建一致的全量执行顺序，便于对照。
//!
//! 变更分两类：
//! - **内容变更**：文件 `content_hash` 变化（含新增/删除）；或构建节点的缓存键变化
//!   （规则命令 / 工具版本 / 声明环境 任一变化）。
//! - **仅时间戳变更**：文件 `content_hash` 未变但 `mtime` 变化。不会触发任何重建。

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::graph::{CompiledGraph, Graph, NodeKind, Rule};

/// 对单个文件的一次观测摘要。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct FileObservation {
    /// 内容哈希（调用方负责，如对文件内容取 sha256）。
    /// 缺省/空串表示该节点在快照中不存在（冷构建/已删除）。
    #[serde(default)]
    pub content_hash: String,
    /// 修改时间，Unix 毫秒。仅用于识别“只 touch 了一下”的文件。
    #[serde(default)]
    pub mtime_ms: i64,
}

/// 一次观测快照。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    /// file 节点 id -> 文件观测。
    #[serde(default)]
    pub files: BTreeMap<String, FileObservation>,
    /// build 节点 id -> 上次的缓存键（由本服务在响应里给出，调用方回传即可）。
    /// 用于识别规则 / 工具版本 / 声明环境的变化。
    #[serde(default)]
    pub build_keys: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PlanRequest {
    pub graph: Graph,
    /// 上一次成功构建时的快照；省略表示冷构建（全量）。
    #[serde(default)]
    pub previous: Option<Snapshot>,
    /// 当前观测。
    pub current: Snapshot,
}

/// 一个构建步骤的变更原因（种子原因；被传播到的节点为 `InputChanged`）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "code", rename_all = "snake_case")]
pub enum Reason {
    /// 冷构建：之前没有该节点的任何记录。
    CacheMiss,
    /// 构建规则本身变化（命令/工具/工具版本/声明环境），附新旧缓存键。
    RuleChanged {
        previous_cache_key: String,
        current_cache_key: String,
    },
    /// 上游有内容变化传播到本节点，附直接触发它的上游节点。
    InputChanged { triggered_by: Vec<String> },
}

/// 单个 build 节点在计划中的状态。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlannedBuild {
    pub node_id: String,
    pub cache_key: String,
    /// 触发重建的原因；无此字段表示缓存命中、本次不执行。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<Reason>,
}

/// 单个 file 节点在计划中的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FileStatus {
    /// 内容未变（“只有时间戳变化”另见响应的 `timestamp_only_files` 列表）。
    Unchanged,
    /// 内容发生变化（哈希变化）。
    ContentChanged,
    /// 新建文件（之前无观测）。
    New,
    /// 文件被删除（当前无观测）。
    Deleted,
    /// 内容本身未变，但它是某个被执行构建动作的产物，将被重新生成。
    Rebuilt,
}

/// 单个 build 节点在计划中的总体分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BuildStatus {
    /// 规则/工具/环境变化。
    RuleChanged,
    /// 上游内容变化传播到此。
    InputChanged,
    /// 冷构建 / 新出现的构建节点。
    CacheMiss,
    /// 无需执行。
    UpToDate,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeState {
    pub kind: NodeKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_status: Option<FileStatus>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub build_status: Option<BuildStatus>,
    /// 是否出现在本次执行集合中（仅 build 节点可能为 true）。
    pub scheduled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PlanStats {
    pub total_nodes: usize,
    pub total_builds: usize,
    /// 本次实际要执行的构建动作数量（菱形共享节点只计一次）。
    pub scheduled_builds: usize,
    /// 冷构建时全量需要执行的构建构建动作数量。
    pub full_build_count: usize,
    /// 仅时间戳变化的文件数量。
    pub timestamp_only_files: usize,
    /// 内容变化的输入文件数量（含新增/删除）。
    pub changed_inputs: usize,
    /// 规则变化（含工具版本/声明环境）的构建节点数量。
    pub changed_rules: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanResponse {
    /// 是否冷构建（previous 缺省或不含任何记录）。
    pub cold_build: bool,
    /// 本次按拓扑序要执行的构建动作 id（已去重）。
    pub steps: Vec<String>,
    /// 若做全量构建，按拓扑序应执行的全部构建动作 id（对照基准）。
    pub full_order: Vec<String>,
    /// 所有受影响节点（种子 + 传播到的下游），含文件与构建，按 id 排序。
    pub affected_nodes: Vec<String>,
    /// 内容发生变化的文件（含新增/删除），按 id 排序。
    pub content_changed_files: Vec<String>,
    /// 哈希未变、仅 mtime 变化的文件——不触发任何重建，按 id 排序。
    pub timestamp_only_files: Vec<String>,
    /// 与依赖图无关的观测文件（图里不存在的 id），按 id 排序。
    pub unrelated_files: Vec<String>,
    /// 每个 build 节点的缓存键与重建原因。
    pub builds: BTreeMap<String, PlannedBuild>,
    /// 全部节点的状态分类。
    pub node_states: BTreeMap<String, NodeState>,
    pub stats: PlanStats,
}

/// 计算 build 节点的缓存键。
///
/// 覆盖：命令、工具名、工具版本、声明环境（按 key 排序后纳入）。
/// 任一项变化都会得到不同的键；输出为 `sha256:` 前缀的十六进制摘要。
pub fn build_cache_key(rule: &Rule) -> String {
    let mut hasher = Sha256::new();
    let mut feed = |tag: &[u8], value: &str| {
        hasher.update(tag);
        hasher.update((value.len() as u64).to_le_bytes());
        hasher.update(value.as_bytes());
    };
    feed(b"command:", &rule.command);
    feed(b"tool:", &rule.tool);
    feed(b"tool_version:", &rule.tool_version);
    // env 用 BTreeMap，迭代顺序即 key 字典序。
    for (k, v) in &rule.env {
        feed(b"env.k:", k);
        feed(b"env.v:", v);
    }
    format!("sha256:{}", hex_encode(&hasher.finalize()))
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// 规划错误（对应 HTTP 422）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanError {
    pub error: String,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub issues: Vec<crate::graph::GraphIssue>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub cycles: Vec<crate::graph::Cycle>,
}

/// 计算构建计划。图本身有静态问题或存在环时返回错误（环可定位）。
pub fn plan(req: PlanRequest) -> Result<PlanResponse, PlanError> {
    let g: CompiledGraph = match req.graph.compile() {
        Ok(g) => g,
        Err(issues) => {
            return Err(PlanError {
                error: "invalid dependency graph".to_string(),
                issues,
                cycles: Vec::new(),
            })
        }
    };

    let cycles = g.find_cycles();
    if !cycles.is_empty() {
        return Err(PlanError {
            error: format!("dependency graph contains {} cycle(s)", cycles.len()),
            issues: Vec::new(),
            cycles,
        });
    }

    let (topo, _remaining) = g.topo_sort();
    let full_order: Vec<String> = topo.iter().filter(|id| g.is_build(id)).cloned().collect();

    let previous = req.previous.unwrap_or_default();
    let current = req.current;

    // 冷构建：previous 完全没有文件观测，也没有任何构建缓存键。
    let cold_build = previous.files.is_empty() && previous.build_keys.is_empty();

    // 无关文件：快照里出现但图中不存在的 id。
    let mut unrelated: Vec<String> = previous
        .files
        .keys()
        .chain(current.files.keys())
        .filter(|id| !g.kinds.contains_key(*id))
        .cloned()
        .collect();
    unrelated.sort();
    unrelated.dedup();

    // ---- 1. 分类文件变更：内容 vs 仅时间戳 ----
    let mut timestamp_only: Vec<String> = Vec::new();
    let mut changed_files: HashSet<String> = HashSet::new();
    let mut file_status: BTreeMap<String, FileStatus> = BTreeMap::new();

    for id in g.node_ids().filter(|id| g.is_file(id)) {
        // 空 content_hash 一律按“无观测”处理（新增/删除识别依赖这个归一化）。
        let prev = previous.files.get(id).filter(|f| nonempty_obs(f));
        let cur = current.files.get(id).filter(|f| nonempty_obs(f));
        let status = match (prev, cur) {
            (None, Some(_)) => FileStatus::New,
            (Some(_), None) => FileStatus::Deleted,
            (Some(p), Some(c)) if p.content_hash != c.content_hash => FileStatus::ContentChanged,
            (Some(p), Some(c)) if p.mtime_ms != c.mtime_ms => {
                timestamp_only.push(id.clone());
                FileStatus::Unchanged
            }
            _ => FileStatus::Unchanged,
        };
        if matches!(
            status,
            FileStatus::New | FileStatus::Deleted | FileStatus::ContentChanged
        ) {
            changed_files.insert(id.clone());
        }
        file_status.insert(id.clone(), status);
    }
    timestamp_only.sort();

    // ---- 2. 分类 build 种子：缓存键变化（规则/工具版本/环境）或冷构建 ----
    let mut keys: BTreeMap<String, String> = BTreeMap::new();
    let mut seed_reason: HashMap<String, Reason> = HashMap::new();
    let mut rule_changed: HashSet<String> = HashSet::new();

    for id in g.node_ids().filter(|id| g.is_build(id)) {
        let rule = g.rules.get(id).expect("validated: build has rule");
        let key = build_cache_key(rule);
        keys.insert(id.clone(), key.clone());

        if cold_build {
            seed_reason.insert(id.clone(), Reason::CacheMiss);
        } else {
            match previous.build_keys.get(id) {
                Some(old) if old != &key => {
                    rule_changed.insert(id.clone());
                    seed_reason.insert(
                        id.clone(),
                        Reason::RuleChanged {
                            previous_cache_key: old.clone(),
                            current_cache_key: key.clone(),
                        },
                    );
                }
                Some(_) => {} // 键相同：规则未变
                None => {
                    // 图里新增的构建节点，或上次没有记录
                    seed_reason.insert(id.clone(), Reason::CacheMiss);
                }
            }
        }
    }

    // ---- 3. 前向传播：内容变更文件 + 规则变更 build 作为种子沿边扩散 ----
    let mut affected: HashSet<String> = HashSet::new();
    let mut queue: Vec<String> = Vec::new();
    let push_affected = |affected: &mut HashSet<String>, queue: &mut Vec<String>, node: String| {
        if affected.insert(node.clone()) {
            queue.push(node);
        }
    };
    for f in &changed_files {
        push_affected(&mut affected, &mut queue, f.clone());
    }
    for id in seed_reason.keys() {
        push_affected(&mut affected, &mut queue, id.clone());
    }

    // 每个被传播到的 build 的直接触发上游（菱形会有两个）。
    let mut triggered_by: HashMap<String, BTreeSet<String>> = HashMap::new();
    while let Some(node) = queue.pop() {
        for next in &g.adj[&node] {
            let was_affected = affected.contains(next);
            affected.insert(next.clone());
            if !was_affected {
                queue.push(next.clone());
            }
            if g.is_build(next) {
                triggered_by
                    .entry(next.clone())
                    .or_default()
                    .insert(node.clone());
            }
        }
    }

    // ---- 4. 执行集合 = 受影响的 build，按全量拓扑序过滤（去重 + 合法顺序）----
    let steps: Vec<String> = full_order
        .iter()
        .filter(|id| affected.contains(*id))
        .cloned()
        .collect();
    let scheduled: HashSet<&str> = steps.iter().map(String::as_str).collect();

    // 受影响的产物文件标记为 Rebuilt（无论当前是否观测到它）。
    for b in &steps {
        for out in &g.adj[b] {
            if g.is_file(out) {
                file_status.insert(out.clone(), FileStatus::Rebuilt);
            }
        }
    }

    // ---- 5. 组装每个 build 的原因与状态 ----
    let mut builds: BTreeMap<String, PlannedBuild> = BTreeMap::new();
    let mut build_status: BTreeMap<String, BuildStatus> = BTreeMap::new();
    for id in g.node_ids().filter(|id| g.is_build(id)) {
        let is_scheduled = scheduled.contains(id.as_str());
        let reason = if let Some(seed) = seed_reason.get(id) {
            Some(seed.clone())
        } else if is_scheduled {
            Some(Reason::InputChanged {
                triggered_by: triggered_by
                    .get(id)
                    .map(|s| s.iter().cloned().collect())
                    .unwrap_or_default(),
            })
        } else {
            None
        };

        let status = match seed_reason.get(id) {
            Some(Reason::RuleChanged { .. }) => BuildStatus::RuleChanged,
            Some(_) => BuildStatus::CacheMiss,
            None if is_scheduled => BuildStatus::InputChanged,
            None => BuildStatus::UpToDate,
        };
        build_status.insert(id.clone(), status);
        builds.insert(
            id.clone(),
            PlannedBuild {
                node_id: id.clone(),
                cache_key: keys[id].clone(),
                reason,
            },
        );
    }

    // ---- 6. 节点状态总表 ----
    let mut node_states: BTreeMap<String, NodeState> = BTreeMap::new();
    for id in g.node_ids() {
        let state = if g.is_file(id) {
            NodeState {
                kind: NodeKind::File,
                file_status: Some(file_status[id]),
                build_status: None,
                scheduled: false,
            }
        } else {
            NodeState {
                kind: NodeKind::Build,
                file_status: None,
                build_status: Some(build_status[id]),
                scheduled: scheduled.contains(id.as_str()),
            }
        };
        node_states.insert(id.clone(), state);
    }

    let mut affected_nodes: Vec<String> = affected.into_iter().collect();
    affected_nodes.sort();
    let mut content_changed_files: Vec<String> = changed_files.into_iter().collect();
    content_changed_files.sort();

    let total_builds = g.node_ids().filter(|id| g.is_build(id)).count();
    let stats = PlanStats {
        total_nodes: g.order.len(),
        total_builds,
        scheduled_builds: steps.len(),
        full_build_count: full_order.len(),
        timestamp_only_files: timestamp_only.len(),
        changed_inputs: content_changed_files.len(),
        changed_rules: rule_changed.len(),
    };

    Ok(PlanResponse {
        cold_build,
        steps,
        full_order,
        affected_nodes,
        content_changed_files,
        timestamp_only_files: timestamp_only,
        unrelated_files: unrelated,
        builds,
        node_states,
        stats,
    })
}

/// 空 content_hash 的观测等同于“无观测”。
fn nonempty_obs(f: &FileObservation) -> bool {
    !f.content_hash.is_empty()
}
