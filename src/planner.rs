//! 核心规划逻辑：
//! - 内容寻址的缓存键（覆盖命令、工具名/版本、声明环境、上游键）
//! - 区分内容变更与仅时间戳变更
//! - 沿依赖边传播影响（菱形依赖天然只重建一次）
//! - 全量构建对照模拟（验证增量结果与全量结果逐产物一致、缓存命中有效）

use std::collections::{BTreeMap, HashMap};

use sha2::{Digest, Sha256};

use crate::graph::{self, Layout};
use crate::model::{
    CycleDto, EdgeDto, FileSummary, InputChange, InputChangeKind, NodeKind, PlanError,
    PlanRequest, PlanResponse, PlanViewDto, Rule, RuleChange, RuleChangeDetail, SimulationDto,
    Snapshot, Step,
};

const LEAF_PREFIX: &str = "leaf-v1|";
const RULE_PREFIX: &str = "rule-v1|";
const TARGET_PREFIX: &str = "target-v1|";

fn sha256_hex(prefixed: &str) -> String {
    let mut h = Sha256::new();
    h.update(prefixed.as_bytes());
    let digest = h.finalize();
    // 手写 hex，避免额外依赖
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// 规则指纹：命令 + 工具名 + 工具版本 + 声明环境。
/// 规则以 serde_json 规范化序列化（environment 为 BTreeMap，键序确定）。
fn rule_fingerprint(rule: &Rule) -> String {
    let canonical = serde_json::to_string(rule).expect("Rule serializes to JSON");
    sha256_hex(&format!("{RULE_PREFIX}{canonical}"))
}

/// 输入（叶子）节点的缓存键仅取决于内容哈希——mtime 不参与。
fn input_key(content_hash: &str) -> String {
    sha256_hex(&format!("{LEAF_PREFIX}{content_hash}"))
}

/// 目标节点缓存键 = 节点 id + 规则指纹 + 所有上游当前键。
fn target_key(node: &str, fp: &str, dep_keys: &[String]) -> String {
    let mut material = format!("{TARGET_PREFIX}{node}|{fp}|");
    for (i, k) in dep_keys.iter().enumerate() {
        if i > 0 {
            material.push(';');
        }
        material.push_str(k);
    }
    sha256_hex(&material)
}

/// 规划入口。
pub fn analyze(req: &PlanRequest) -> Result<PlanResponse, PlanError> {
    let layout = graph::prepare(&req.graph).map_err(|e| match e {
        graph::GraphError::Invalid(errs) => PlanError::InvalidRequest { errors: errs },
        graph::GraphError::Cycle(c) => PlanError::CycleDetected {
            cycle: CycleDto {
                nodes: c.nodes,
                edges: c.edges.into_iter().map(|e| EdgeDto { from: e.from, to: e.to }).collect(),
            },
        },
    })?;

    let Layout { order, kinds, deps, rules } = layout;

    let input_nodes: Vec<&str> = order
        .iter()
        .filter(|id| kinds.get(*id) == Some(&NodeKind::Input))
        .map(String::as_str)
        .collect();
    let target_nodes: Vec<&str> = order
        .iter()
        .filter(|id| kinds.get(*id) == Some(&NodeKind::Target))
        .map(String::as_str)
        .collect();

    // ---- 校验：每个 input 节点必须有当前文件摘要 -------------------------
    let mut errs: Vec<String> = Vec::new();
    for id in &input_nodes {
        if !req.files.contains_key(*id) {
            errs.push(format!("missing current file summary for input node '{id}'"));
        }
    }
    if !errs.is_empty() {
        return Err(PlanError::InvalidRequest { errors: errs });
    }

    // ---- files 中未被图引用的条目（无关文件） ---------------------------
    let input_set: std::collections::HashSet<&str> = input_nodes.iter().copied().collect();
    let target_set: std::collections::HashSet<&str> = target_nodes.iter().copied().collect();
    let mut ignored_files: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();
    for key in req.files.keys() {
        if !input_set.contains(key.as_str()) {
            ignored_files.push(key.clone());
            if target_set.contains(key.as_str()) {
                warnings.push(format!(
                    "file summary for '{key}' ignored: it is a target node, not an input"
                ));
            }
        }
    }

    // ---- 规则指纹 --------------------------------------------------------
    let mut fingerprints: HashMap<&str, String> = HashMap::new();
    for t in &target_nodes {
        let rule = rules.get(*t).expect("target rule validated in graph::prepare");
        fingerprints.insert(*t, rule_fingerprint(rule));
    }

    // ---- 当前键：按拓扑顺序计算 ------------------------------------------
    let mut current_keys: HashMap<&str, String> = HashMap::new();
    for id in &order {
        let key = match kinds.get(id.as_str()) {
            Some(NodeKind::Input) => {
                let f = req.files.get(id.as_str()).expect("file presence validated");
                input_key(&f.content_hash)
            }
            Some(NodeKind::Target) => {
                let fp = fingerprints.get(id.as_str()).expect("fingerprint present");
                let dep_keys: Vec<String> = deps
                    .get(id.as_str())
                    .map(|ds| {
                        ds.iter()
                            .map(|d| current_keys.get(d.as_str()).expect("dep key computed in topo order").clone())
                            .collect()
                    })
                    .unwrap_or_default();
                target_key(id, fp, &dep_keys)
            }
            None => unreachable!("all layout nodes have a kind"),
        };
        current_keys.insert(id.as_str(), key);
    }

    let cold = req.baseline.is_none();
    let baseline = req.baseline.as_ref();

    // 基线中某节点“上一次的键”（输入由基线文件摘要重算，目标直接读取）。
    let baseline_node_key = |node: &str| -> Option<String> {
        let b = baseline?;
        match kinds.get(node) {
            Some(NodeKind::Input) => b.files.get(node).map(|f| input_key(&f.content_hash)),
            Some(NodeKind::Target) => b.target_keys.get(node).cloned(),
            None => None,
        }
    };

    // ---- 输入变更（内容 vs 仅时间戳） ------------------------------------
    let mut changed_inputs: Vec<InputChange> = Vec::new();
    let mut timestamp_only_inputs: Vec<String> = Vec::new();
    let mut input_content_changed: std::collections::HashSet<String> = std::collections::HashSet::new();

    if let Some(b) = baseline {
        for id in &input_nodes {
            let cur: &FileSummary = req.files.get(*id).expect("file present");
            match b.files.get(*id) {
                None => {
                    changed_inputs.push(InputChange {
                        node: id.to_string(),
                        kind: InputChangeKind::NewFile,
                        from: None,
                        to: cur.clone(),
                    });
                    input_content_changed.insert(id.to_string());
                }
                Some(old) => {
                    if old.content_hash != cur.content_hash {
                        changed_inputs.push(InputChange {
                            node: id.to_string(),
                            kind: InputChangeKind::ContentChanged,
                            from: Some(old.clone()),
                            to: cur.clone(),
                        });
                        input_content_changed.insert(id.to_string());
                    } else if old.mtime != cur.mtime {
                        changed_inputs.push(InputChange {
                            node: id.to_string(),
                            kind: InputChangeKind::TimestampOnly,
                            from: Some(old.clone()),
                            to: cur.clone(),
                        });
                        timestamp_only_inputs.push(id.to_string());
                    }
                }
            }
        }
    }

    // ---- 规则变更 --------------------------------------------------------
    let mut rule_changes: Vec<RuleChange> = Vec::new();
    let mut rule_changed_nodes: std::collections::HashSet<String> = std::collections::HashSet::new();

    if let Some(b) = baseline {
        for t in &target_nodes {
            let cur = rules.get(*t).expect("rule present");
            match b.rules.get(*t) {
                None => {
                    rule_changes.push(RuleChange {
                        node: t.to_string(),
                        details: vec![RuleChangeDetail::NewRule],
                        from: None,
                        to: cur.clone(),
                    });
                    rule_changed_nodes.insert(t.to_string());
                }
                Some(old) if old != cur => {
                    let details = rule_diff(old, cur);
                    rule_changes.push(RuleChange {
                        node: t.to_string(),
                        details,
                        from: Some(old.clone()),
                        to: cur.clone(),
                    });
                    rule_changed_nodes.insert(t.to_string());
                }
                Some(_) => {}
            }
        }
    }

    // ---- 影响传播（沿依赖边，拓扑序单趟） --------------------------------
    //
    // 每个目标节点在拓扑序中只访问一次：菱形汇合点即使有两条变更路径，
    // 也只会产生一个步骤（集合去重 + 单趟计算）。
    let mut affected: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut reasons: HashMap<String, String> = HashMap::new();

    for t in &target_nodes {
        let cur_key = current_keys.get(*t).expect("key present");

        if cold {
            affected.insert(t.to_string());
            reasons.insert(t.to_string(), "cold_build".to_string());
            continue;
        }

        // 自身规则变更优先作为直接原因。
        if rule_changed_nodes.contains(*t) {
            affected.insert(t.to_string());
            reasons.insert(t.to_string(), "rule_changed".to_string());
            continue;
        }

        // 基线里没有该目标的键（新目标）。
        let base_target_key = baseline.and_then(|b| b.target_keys.get(*t).cloned());
        if base_target_key.is_none() {
            affected.insert(t.to_string());
            reasons.insert(t.to_string(), "new_target".to_string());
            continue;
        }

        // 上游键变化（含输入内容变化与被影响的目标）。
        let dep_changed = deps.get(*t).map(|ds| {
            ds.iter().any(|d| {
                let cur_dep = current_keys.get(d.as_str()).expect("dep key present");
                match baseline_node_key(d) {
                    Some(base_dep) => cur_dep != &base_dep,
                    None => true, // 上游也是新增节点
                }
            })
        }).unwrap_or(false);

        if dep_changed {
            affected.insert(t.to_string());
            reasons.insert(t.to_string(), "dependency_changed".to_string());
        } else if cur_key != base_target_key.as_ref().expect("checked above") {
            // 防御性兜底：自身键变了但规则/上游都没变，理论上不会发生。
            affected.insert(t.to_string());
            reasons.insert(t.to_string(), "key_changed".to_string());
        }
    }

    // ---- 构建步骤视图 ----------------------------------------------------
    let full_steps: Vec<Step> = target_nodes
        .iter()
        .map(|t| Step {
            node: t.to_string(),
            reason: None,
            cache_key: current_keys.get(*t).expect("key").clone(),
            rule_fingerprint: fingerprints.get(*t).expect("fp").clone(),
        })
        .collect();

    let inc_steps: Vec<Step> = target_nodes
        .iter()
        .filter(|t| affected.contains(**t))
        .map(|t| Step {
            node: t.to_string(),
            reason: Some(reasons.get(*t).cloned().unwrap_or_else(|| "affected".to_string())),
            cache_key: current_keys.get(*t).expect("key").clone(),
            rule_fingerprint: fingerprints.get(*t).expect("fp").clone(),
        })
        .collect();

    let skipped: Vec<String> = target_nodes
        .iter()
        .filter(|t| !affected.contains(**t))
        .map(|t| t.to_string())
        .collect();

    // ---- 产物模拟：增量 vs 全量 ------------------------------------------
    //
    // 模拟约定：本服务不执行真实命令，产物摘要定义为内容寻址的目标键。
    // 全量构建 = 用当前键重算所有目标；
    // 增量构建 = 受影响目标用当前键，跳过目标沿用基线产物。
    let full_artifacts: BTreeMap<String, String> = target_nodes
        .iter()
        .map(|t| (t.to_string(), current_keys.get(*t).expect("key").clone()))
        .collect();

    let mut incremental_artifacts: BTreeMap<String, String> = BTreeMap::new();
    let mut cache_hits_valid = true;
    for t in &target_nodes {
        let cur = current_keys.get(*t).expect("key").clone();
        if affected.contains(*t) {
            incremental_artifacts.insert(t.to_string(), cur);
        } else {
            // 跳过：沿用基线产物；同时验证基线键与当前键一致（命中才允许跳过）。
            match baseline.and_then(|b| b.target_keys.get(*t).cloned()) {
                Some(base) => {
                    if base != cur {
                        cache_hits_valid = false;
                    }
                    incremental_artifacts.insert(t.to_string(), base);
                }
                None => {
                    // 冷启动不存在跳过；防御性处理。
                    cache_hits_valid = false;
                    incremental_artifacts.insert(t.to_string(), cur);
                }
            }
        }
    }

    let mismatches: Vec<String> = full_artifacts
        .iter()
        .filter(|(k, v)| incremental_artifacts.get(*k) != Some(v))
        .map(|(k, _)| k.clone())
        .collect();
    let equivalent = mismatches.is_empty();

    // ---- 基线冗余条目提示 ------------------------------------------------
    if let Some(b) = baseline {
        let mut extra: Vec<String> = Vec::new();
        for k in b.files.keys() {
            if !input_set.contains(k.as_str()) {
                extra.push(format!("baseline file '{k}' is not an input node in the current graph"));
            }
        }
        for k in b.rules.keys() {
            if !target_set.contains(k.as_str()) {
                extra.push(format!("baseline rule '{k}' is not a target node in the current graph"));
            }
        }
        for k in b.target_keys.keys() {
            if !target_set.contains(k.as_str()) {
                extra.push(format!(
                    "baseline target key '{k}' is not a target node in the current graph"
                ));
            }
        }
        extra.sort();
        warnings.extend(extra);
    }

    // ---- 下一次快照 ------------------------------------------------------
    let mut snap_files: BTreeMap<String, FileSummary> = BTreeMap::new();
    for id in &input_nodes {
        if let Some(f) = req.files.get(*id) {
            snap_files.insert(id.to_string(), f.clone());
        }
    }
    let mut snap_rules: BTreeMap<String, Rule> = BTreeMap::new();
    let mut snap_keys: BTreeMap<String, String> = BTreeMap::new();
    for t in &target_nodes {
        snap_rules.insert(t.to_string(), rules.get(*t).expect("rule").clone());
        snap_keys.insert(t.to_string(), current_keys.get(*t).expect("key").clone());
    }

    let affected_targets: Vec<String> =
        target_nodes.iter().filter(|t| affected.contains(**t)).map(|t| t.to_string()).collect();

    let inc_step_count = inc_steps.len();
    let full_step_count = full_steps.len();
    let skipped_count = skipped.len();
    let sim_incremental_artifacts = incremental_artifacts.clone();
    let sim_full_artifacts = full_artifacts.clone();

    Ok(PlanResponse {
        status: if cold { "full_build" } else { "incremental" }.to_string(),
        topological_order: order,
        changed_inputs,
        timestamp_only_inputs,
        rule_changes,
        affected_targets,
        incremental: PlanViewDto {
            steps: inc_steps,
            step_count: inc_step_count,
            skipped,
            skipped_count,
            artifacts: incremental_artifacts,
        },
        full_build: PlanViewDto {
            steps: full_steps,
            step_count: full_step_count,
            skipped: Vec::new(),
            skipped_count: 0,
            artifacts: full_artifacts,
        },
        simulation: SimulationDto {
            incremental_artifacts: sim_incremental_artifacts,
            full_artifacts: sim_full_artifacts,
            equivalent,
            mismatches,
            cache_hits_valid,
        },
        next_snapshot: Snapshot {
            files: snap_files,
            rules: snap_rules,
            target_keys: snap_keys,
        },
        ignored_files,
        warnings,
    })
}

/// 规则字段级差异。
fn rule_diff(old: &Rule, new: &Rule) -> Vec<RuleChangeDetail> {
    let mut d = Vec::new();
    if old.command != new.command {
        d.push(RuleChangeDetail::CommandChanged);
    }
    let old_name = old.tool.as_ref().map(|t| t.name.as_str());
    let new_name = new.tool.as_ref().map(|t| t.name.as_str());
    if old_name != new_name {
        d.push(RuleChangeDetail::ToolNameChanged);
    }
    let old_ver = old.tool.as_ref().map(|t| t.version.as_str());
    let new_ver = new.tool.as_ref().map(|t| t.version.as_str());
    if old_ver != new_ver {
        d.push(RuleChangeDetail::ToolVersionChanged);
    }
    if old.environment != new.environment {
        d.push(RuleChangeDetail::EnvironmentChanged);
    }
    d
}

#[cfg(test)]
mod key_tests {
    use super::*;
    use crate::model::Tool;

    fn tool(name: &str, version: &str) -> Tool {
        Tool { name: name.into(), version: version.into() }
    }

    fn rule_v(command: &str, version: &str, cc: &str) -> Rule {
        let mut env = BTreeMap::new();
        env.insert("CC".to_string(), cc.to_string());
        Rule {
            command: command.to_string(),
            tool: Some(tool("gcc", version)),
            environment: env,
        }
    }

    #[test]
    fn input_key_ignores_mtime() {
        // mtime 不参与键：同一内容哈希即便来自不同摘要也得到相同键。
        assert_eq!(input_key("abc"), input_key("abc"));
        assert_ne!(input_key("abc"), input_key("abd"));
    }

    #[test]
    fn fingerprint_covers_command_tool_version_and_env() {
        let base = rule_v("cc -o x", "13.2.0", "gcc");
        assert_eq!(rule_fingerprint(&base), rule_fingerprint(&base.clone()));
        assert_ne!(rule_fingerprint(&base), rule_fingerprint(&rule_v("cc -o y", "13.2.0", "gcc")));
        assert_ne!(rule_fingerprint(&base), rule_fingerprint(&rule_v("cc -o x", "14.0.0", "gcc")));
        assert_ne!(rule_fingerprint(&base), rule_fingerprint(&rule_v("cc -o x", "13.2.0", "clang")));
        assert_ne!(
            rule_fingerprint(&base),
            rule_fingerprint(&Rule {
                command: base.command.clone(),
                tool: None,
                environment: BTreeMap::new(),
            })
        );
    }

    #[test]
    fn target_key_covers_fingerprint_and_dependency_keys() {
        let fp = rule_fingerprint(&rule_v("cc", "13", "gcc"));
        let k1 = target_key("t", &fp, &[input_key("d1")]);
        assert_eq!(k1, target_key("t", &fp, &[input_key("d1")]));
        // 上游键变化 -> 目标键变化
        assert_ne!(k1, target_key("t", &fp, &[input_key("d2")]));
        // 规则变化 -> 目标键变化
        let fp2 = rule_fingerprint(&rule_v("cc", "14", "gcc"));
        assert_ne!(k1, target_key("t", &fp2, &[input_key("d1")]));
        // 依赖顺序不同（语义上也是不同输入）
        assert_ne!(
            target_key("t", &fp, &[input_key("d1"), input_key("d2")]),
            target_key("t", &fp, &[input_key("d2"), input_key("d1")])
        );
        // 键为 hex sha256
        assert_eq!(k1.len(), 64);
        assert!(k1.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
