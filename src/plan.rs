//! 合并规划：把多个动作的输出条目并入一棵虚拟树，检测全部冲突。
//!
//! 规划阶段完全不触碰文件系统，形成的 [`Plan`] 是落盘阶段的唯一依据。
//! 冲突包括：文件/目录互斥、同路径异内容（含文件与符号链接互混）、
//! 大小写折叠碰撞、符号链接目标逃逸、非法路径。
//!
//! # 树结构
//!
//! 每层孩子以「大小写折叠后的名字」为键存入 [`CaseBucket`]，桶内再按
//! 原始拼写区分变体：
//! - 折叠键相同、原始拼写也相同 → 同一路径，做内容合并；
//! - 折叠键相同、原始拼写不同 → 大小写折叠碰撞（两个变体都保留以便继续分析）。

use std::collections::HashSet;

use base64::Engine;
use indexmap::IndexMap;
use sha2::{Digest, Sha256};

use crate::model::*;
use crate::paths;

/// 合并树中的节点类型。
#[derive(Debug, Clone)]
enum NodeKind {
    /// 显式目录，记录所有声明它的动作（按首次出现顺序）。
    Dir { actions: Vec<String> },
    /// 文件，携带解码后字节的 sha256 与大小。
    File {
        sha256: String,
        size: u64,
        actions: Vec<String>,
    },
    /// 符号链接，携带规范化后的目标。
    Symlink {
        target: String,
        actions: Vec<String>,
    },
    /// 由更深条目路径隐含产生的中间目录。
    ImplicitDir { introduced_by: String },
}

impl NodeKind {
    /// 与该节点相关的来源动作。
    fn actions(&self) -> Vec<String> {
        match self {
            NodeKind::Dir { actions }
            | NodeKind::File { actions, .. }
            | NodeKind::Symlink { actions, .. } => actions.clone(),
            NodeKind::ImplicitDir { introduced_by } => vec![introduced_by.clone()],
        }
    }
}

#[derive(Debug, Clone)]
struct Node {
    kind: NodeKind,
    children: IndexMap<String, CaseBucket>,
}

/// 同一大小写折叠键下的不同原始拼写变体。
#[derive(Debug, Clone)]
struct CaseBucket {
    /// 键为保留大小写的精确名字，值为子树。
    variants: IndexMap<String, Node>,
}

impl CaseBucket {
    fn empty() -> Self {
        CaseBucket {
            variants: IndexMap::new(),
        }
    }
}

/// 请求级别错误（结构问题，区别于合并冲突）。
#[derive(Debug)]
pub enum PlanError {
    /// HTTP 400
    BadRequest(String),
}

/// 依据请求构建完整合并计划。
pub fn build_plan(req: &MergeRequest) -> Result<Plan, PlanError> {
    if req.actions.is_empty() {
        return Err(PlanError::BadRequest("`actions` must not be empty".into()));
    }
    let mut seen_ids = HashSet::new();
    for a in &req.actions {
        if a.id.is_empty() {
            return Err(PlanError::BadRequest("action id must not be empty".into()));
        }
        if !seen_ids.insert(a.id.clone()) {
            return Err(PlanError::BadRequest(format!(
                "duplicate action id: {}",
                a.id
            )));
        }
    }

    let mut root = Node {
        kind: NodeKind::ImplicitDir {
            introduced_by: req.actions[0].id.clone(),
        },
        children: IndexMap::new(),
    };
    let mut conflicts: Vec<Conflict> = Vec::new();
    // 同 (类型, 路径) 的冲突只报告一次。
    let mut reported: HashSet<(ConflictKind, String)> = HashSet::new();

    for action in &req.actions {
        for entry in &action.outputs {
            process_entry(&mut root, &action.id, entry, &mut conflicts, &mut reported);
        }
    }

    // 展开虚拟树为有序计划。
    let mut plan = Plan {
        target_dir: req.target_dir.clone(),
        conflicts,
        ..Default::default()
    };
    flatten(&root, &mut Vec::new(), &mut plan);
    plan.dirs.sort_keys();
    plan.files.sort_keys();
    plan.symlinks.sort_keys();
    // 冲突按深度（段数）升序排列，保证「最短冲突路径」排在最前；
    // 同深度按路径、类型排序，输出稳定。
    plan.conflicts.sort_by(|a, b| {
        depth(&a.path)
            .cmp(&depth(&b.path))
            .then_with(|| a.path.cmp(&b.path))
            .then_with(|| format!("{:?}", a.kind).cmp(&format!("{:?}", b.kind)))
    });
    Ok(plan)
}

fn depth(path: &str) -> usize {
    if path.is_empty() {
        0
    } else {
        path.matches('/').count() + 1
    }
}

fn process_entry(
    root: &mut Node,
    action_id: &str,
    entry: &OutputEntry,
    conflicts: &mut Vec<Conflict>,
    reported: &mut HashSet<(ConflictKind, String)>,
) {
    let segs = match paths::split_validated(&entry.path) {
        Ok(segs) => segs,
        Err(detail) => {
            report(
                conflicts,
                reported,
                ConflictKind::InvalidPath,
                entry.path.clone(),
                vec![action_id.to_string()],
                detail,
            );
            return;
        }
    };
    let path = segs.join("/");

    // 类型相关的负载校验。
    let symlink_target = if entry.kind == EntryKind::Symlink {
        match entry.target.as_deref() {
            Some(t) => match paths::normalize_symlink_target(&segs, t) {
                Ok(n) => Some(n),
                Err(detail) => {
                    report(
                        conflicts,
                        reported,
                        ConflictKind::SymlinkEscape,
                        path,
                        vec![action_id.to_string()],
                        detail,
                    );
                    return;
                }
            },
            None => {
                report(
                    conflicts,
                    reported,
                    ConflictKind::SymlinkEscape,
                    path,
                    vec![action_id.to_string()],
                    "symlink entry is missing `target`".to_string(),
                );
                return;
            }
        }
    } else {
        None
    };

    let file_hash = if entry.kind == EntryKind::File {
        let raw = entry.content_base64.as_deref().unwrap_or("");
        match base64::engine::general_purpose::STANDARD.decode(raw) {
            Ok(bytes) => {
                let mut hasher = Sha256::new();
                hasher.update(&bytes);
                Some((hex(hasher.finalize()), bytes.len() as u64))
            }
            Err(e) => {
                report(
                    conflicts,
                    reported,
                    ConflictKind::InvalidPath,
                    path,
                    vec![action_id.to_string()],
                    format!("invalid base64 content: {e}"),
                );
                return;
            }
        }
    } else {
        None
    };

    insert_entry(
        root,
        &segs,
        action_id,
        entry.kind,
        symlink_target,
        file_hash,
        conflicts,
        reported,
    );
}

#[allow(clippy::too_many_arguments)]
fn insert_entry(
    root: &mut Node,
    segs: &[String],
    action_id: &str,
    kind: EntryKind,
    symlink_target: Option<String>,
    file_hash: Option<(String, u64)>,
    conflicts: &mut Vec<Conflict>,
    reported: &mut HashSet<(ConflictKind, String)>,
) {
    let mut cur = root;
    let mut built: Vec<String> = Vec::new();

    for (depth_idx, seg) in segs.iter().enumerate() {
        built.push(seg.clone());
        let is_leaf = depth_idx == segs.len() - 1;
        let folded = paths::fold_segment(seg);

        // 沿折叠键定位桶；再按精确拼写定位变体。
        let bucket = cur
            .children
            .entry(folded.clone())
            .or_insert_with(CaseBucket::empty);
        let new_spelling = !bucket.variants.contains_key(seg);
        if new_spelling {
            bucket.variants.insert(
                seg.clone(),
                Node {
                    kind: NodeKind::ImplicitDir {
                        introduced_by: action_id.to_string(),
                    },
                    children: IndexMap::new(),
                },
            );
            // 桶里出现第二个不同拼写 → 大小写折叠碰撞。
            if bucket.variants.len() >= 2 {
                let mut actions = Vec::new();
                for v in bucket.variants.values() {
                    for a in v.kind.actions() {
                        push_unique(&mut actions, &a);
                    }
                }
                push_unique(&mut actions, action_id);
                // 冲突路径取桶内第一个变体的完整路径（同深度，同为最短路径）。
                let first_spelling = bucket.variants.keys().next().unwrap().clone();
                let mut collided = built.clone();
                *collided.last_mut().unwrap() = first_spelling;
                report(
                    conflicts,
                    reported,
                    ConflictKind::CaseFoldCollision,
                    collided.join("/"),
                    actions,
                    format!(
                        "case-folded name collision in {}: names {:?} collide",
                        parent_dir(&built),
                        bucket
                            .variants
                            .keys()
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ")
                    ),
                );
            }
        }
        let node = bucket.variants.get_mut(seg).unwrap();
        cur = node;

        if !is_leaf {
            // 中间段必须具备目录语义；文件 / 符号链接挡路即冲突。
            if matches!(cur.kind, NodeKind::File { .. } | NodeKind::Symlink { .. }) {
                let mut actions = cur.kind.actions();
                push_unique(&mut actions, action_id);
                report(
                    conflicts,
                    reported,
                    ConflictKind::FileDirConflict,
                    built.join("/"),
                    actions,
                    format!(
                        "{action_id}: path {} must be a directory for {} but it is a file/symlink",
                        built.join("/"),
                        segs.join("/")
                    ),
                );
                return;
            }
        }
    }

    // cur 为叶子节点，按类型合并。
    merge_leaf(
        cur,
        &built.join("/"),
        action_id,
        kind,
        symlink_target,
        file_hash,
        conflicts,
        reported,
    );
}

#[allow(clippy::too_many_arguments)]
fn merge_leaf(
    node: &mut Node,
    path: &str,
    action_id: &str,
    kind: EntryKind,
    symlink_target: Option<String>,
    file_hash: Option<(String, u64)>,
    conflicts: &mut Vec<Conflict>,
    reported: &mut HashSet<(ConflictKind, String)>,
) {
    let blocked_by_children =
        !node.children.is_empty() && matches!(kind, EntryKind::File | EntryKind::Symlink);

    match kind {
        EntryKind::Dir => match &node.kind {
            NodeKind::Dir { .. } => {
                if let NodeKind::Dir { actions } = &mut node.kind {
                    push_unique(actions, action_id);
                }
            }
            NodeKind::ImplicitDir { .. } => {
                node.kind = NodeKind::Dir {
                    actions: vec![action_id.to_string()],
                };
            }
            NodeKind::File { .. } | NodeKind::Symlink { .. } => {
                let mut actions = node.kind.actions();
                push_unique(&mut actions, action_id);
                report(
                    conflicts,
                    reported,
                    ConflictKind::FileDirConflict,
                    path.to_string(),
                    actions,
                    format!(
                        "{action_id}: directory {path} conflicts with an existing file/symlink"
                    ),
                );
            }
        },
        EntryKind::File => {
            let (sha, size) = file_hash.unwrap();
            if blocked_by_children {
                let mut actions = vec![action_id.to_string()];
                for bucket in node.children.values() {
                    for v in bucket.variants.values() {
                        for a in v.kind.actions() {
                            push_unique(&mut actions, &a);
                        }
                    }
                }
                report(
                    conflicts,
                    reported,
                    ConflictKind::FileDirConflict,
                    path.to_string(),
                    actions,
                    format!(
                        "{action_id}: file {path} conflicts with entries nested beneath that path"
                    ),
                );
                return;
            }
            match &node.kind {
                NodeKind::Dir { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::FileDirConflict,
                        path.to_string(),
                        actions,
                        format!("{action_id}: file {path} conflicts with an existing directory"),
                    );
                }
                NodeKind::File { sha256, .. } if *sha256 == sha => {
                    // 同路径同内容：共享，不冲突。
                    if let NodeKind::File { actions, .. } = &mut node.kind {
                        push_unique(actions, action_id);
                    }
                }
                NodeKind::File { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::ContentConflict,
                        path.to_string(),
                        actions,
                        format!("actions produce different file contents at {path}"),
                    );
                }
                NodeKind::Symlink { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::ContentConflict,
                        path.to_string(),
                        actions,
                        format!("{action_id}: file {path} conflicts with an existing symlink"),
                    );
                }
                NodeKind::ImplicitDir { .. } => {
                    node.kind = NodeKind::File {
                        sha256: sha,
                        size,
                        actions: vec![action_id.to_string()],
                    };
                }
            }
        }
        EntryKind::Symlink => {
            let target = symlink_target.unwrap();
            if blocked_by_children {
                let mut actions = vec![action_id.to_string()];
                for bucket in node.children.values() {
                    for v in bucket.variants.values() {
                        for a in v.kind.actions() {
                            push_unique(&mut actions, &a);
                        }
                    }
                }
                report(
                    conflicts,
                    reported,
                    ConflictKind::FileDirConflict,
                    path.to_string(),
                    actions,
                    format!(
                        "{action_id}: symlink {path} conflicts with entries nested beneath that path"
                    ),
                );
                return;
            }
            match &node.kind {
                NodeKind::Dir { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::FileDirConflict,
                        path.to_string(),
                        actions,
                        format!("{action_id}: symlink {path} conflicts with an existing directory"),
                    );
                }
                NodeKind::Symlink { target: old, .. } if *old == target => {
                    if let NodeKind::Symlink { actions, .. } = &mut node.kind {
                        push_unique(actions, action_id);
                    }
                }
                NodeKind::Symlink { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::ContentConflict,
                        path.to_string(),
                        actions,
                        format!("actions produce different symlink targets at {path}"),
                    );
                }
                NodeKind::File { .. } => {
                    let mut actions = node.kind.actions();
                    push_unique(&mut actions, action_id);
                    report(
                        conflicts,
                        reported,
                        ConflictKind::ContentConflict,
                        path.to_string(),
                        actions,
                        format!("{action_id}: symlink {path} conflicts with an existing file"),
                    );
                }
                NodeKind::ImplicitDir { .. } => {
                    node.kind = NodeKind::Symlink {
                        target,
                        actions: vec![action_id.to_string()],
                    };
                }
            }
        }
    }
}

fn parent_dir(built: &[String]) -> String {
    if built.len() <= 1 {
        "<root>".to_string()
    } else {
        built[..built.len() - 1].join("/")
    }
}

fn push_unique(actions: &mut Vec<String>, id: &str) {
    if !actions.iter().any(|a| a == id) {
        actions.push(id.to_string());
    }
}

fn report(
    conflicts: &mut Vec<Conflict>,
    reported: &mut HashSet<(ConflictKind, String)>,
    kind: ConflictKind,
    path: String,
    actions: Vec<String>,
    detail: String,
) {
    if reported.insert((kind, path.clone())) {
        conflicts.push(Conflict {
            kind,
            path,
            actions,
            detail,
        });
    }
}

pub(crate) fn hex(bytes: impl AsRef<[u8]>) -> String {
    bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

/// apply 模块复用的摘要编码。
pub(crate) fn plan_hex(bytes: impl AsRef<[u8]>) -> String {
    hex(bytes)
}

/// 深度优先展开虚拟树，生成计划中的目录 / 文件 / 符号链接清单。
fn flatten(node: &Node, prefix: &mut Vec<String>, plan: &mut Plan) {
    for (folded, bucket) in &node.children {
        for (exact, child) in &bucket.variants {
            let _ = folded;
            prefix.push(exact.clone());
            let p = prefix.join("/");
            match &child.kind {
                NodeKind::Dir { actions } => {
                    plan.dirs.insert(
                        p.clone(),
                        PlannedDir {
                            actions: actions.clone(),
                        },
                    );
                }
                NodeKind::ImplicitDir { introduced_by } => {
                    plan.dirs.insert(
                        p.clone(),
                        PlannedDir {
                            actions: vec![introduced_by.clone()],
                        },
                    );
                }
                NodeKind::File {
                    sha256,
                    size,
                    actions,
                } => {
                    plan.files.insert(
                        p.clone(),
                        PlannedFile {
                            sha256: sha256.clone(),
                            actions: actions.clone(),
                            size: *size,
                        },
                    );
                }
                NodeKind::Symlink { target, actions } => {
                    plan.symlinks.insert(
                        p.clone(),
                        PlannedSymlink {
                            target: target.clone(),
                            actions: actions.clone(),
                        },
                    );
                }
            }
            flatten(child, prefix, plan);
            prefix.pop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(path: &str, content: &str) -> OutputEntry {
        OutputEntry {
            path: path.into(),
            kind: EntryKind::File,
            content_base64: Some(base64::engine::general_purpose::STANDARD.encode(content)),
            target: None,
        }
    }

    fn req(actions: Vec<Action>) -> MergeRequest {
        MergeRequest {
            target_dir: "/tmp/o".into(),
            actions,
        }
    }

    #[test]
    fn file_vs_nested_dir_conflict() {
        let plan = build_plan(&req(vec![
            Action {
                id: "act1".into(),
                outputs: vec![file("a", "x")],
            },
            Action {
                id: "act2".into(),
                outputs: vec![file("a/b", "y")],
            },
        ]))
        .unwrap();
        let c = &plan.conflicts[0];
        assert_eq!(c.kind, ConflictKind::FileDirConflict);
        assert_eq!(c.path, "a"); // 最短冲突路径
        assert_eq!(c.actions, vec!["act1", "act2"]);
    }
}
