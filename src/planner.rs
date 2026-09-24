//! Deterministic, side-effect-free merge planning.
//!
//! All outputs from all actions are gathered and validated into a complete
//! [`Plan`] before anything is written. The applier only runs when
//! `plan.conflicts` is empty, so a rejected merge never mutates the target
//! tree.

use std::collections::{BTreeMap, BTreeSet};

use sha2::{Digest, Sha256};

use crate::model::*;
use crate::path::{normalize_target, RelPath};

pub fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

fn kind_rank(k: EntryKind) -> u8 {
    match k {
        EntryKind::Dir => 0,
        EntryKind::Symlink => 1,
        EntryKind::File => 2,
    }
}

fn union_actions(prov_sets: &[&[Prov]]) -> Vec<String> {
    let mut s: BTreeSet<String> = BTreeSet::new();
    for ps in prov_sets {
        for p in ps.iter() {
            s.insert(p.action.clone());
        }
    }
    s.into_iter().collect()
}

/// Build the full plan for a set of named actions.
///
/// `actions` maps action id -> its declared outputs (order preserved).
/// `case_sensitive` disables case-folding checks when true.
pub fn build_plan(actions: &BTreeMap<String, Vec<OutputSpec>>, case_sensitive: bool) -> Plan {
    let action_ids: Vec<String> = actions.keys().cloned().collect();
    let mut conflicts: Vec<Conflict> = Vec::new();

    // --- Phase 1: gather outputs by exact path -----------------------------
    let mut by_path: BTreeMap<RelPath, Vec<(Prov, OutputSpec)>> = BTreeMap::new();
    for (action_id, outputs) in actions {
        for (index, spec) in outputs.iter().enumerate() {
            by_path.entry(spec.path.clone()).or_default().push((
                Prov {
                    action: action_id.clone(),
                    index,
                },
                spec.clone(),
            ));
        }
    }

    // --- Phase 2: per-path resolution + same-path incompatibility -----------
    let mut nodes: BTreeMap<RelPath, PlannedNode> = BTreeMap::new();
    for (path, specs) in &by_path {
        let mut kinds: BTreeSet<EntryKind> = BTreeSet::new();
        let mut file_hashes: BTreeSet<String> = BTreeSet::new();
        let mut link_targets: BTreeSet<String> = BTreeSet::new();
        let mut provs: Vec<Prov> = specs.iter().map(|(p, _)| p.clone()).collect();
        provs.sort();

        let mut chosen: Option<&OutputSpec> = None;
        for (_, spec) in specs {
            kinds.insert(spec.kind);
            if let Some(h) = &spec.content_hash {
                file_hashes.insert(h.clone());
            }
            if let Some(t) = &spec.link_target {
                link_targets.insert(t.clone());
            }
            if chosen.is_none() || kind_rank(spec.kind) > kind_rank(chosen.unwrap().kind) {
                chosen = Some(spec);
            }
        }

        let kind_conflict = kinds.len() > 1;
        let content_conflict = kinds.contains(&EntryKind::File) && file_hashes.len() > 1;
        let link_conflict = kinds.contains(&EntryKind::Symlink) && link_targets.len() > 1;

        if kind_conflict || content_conflict || link_conflict {
            let detail = if kind_conflict {
                let names: Vec<&str> = kinds.iter().map(|k| k.as_str()).collect();
                format!(
                    "path is produced with incompatible kinds: {}",
                    names.join(" vs ")
                )
            } else if content_conflict {
                format!(
                    "path is produced as a file with {} distinct contents (sha256: {})",
                    file_hashes.len(),
                    file_hashes.iter().cloned().collect::<Vec<_>>().join(", ")
                )
            } else {
                format!(
                    "path is produced as a symlink with {} distinct targets: [{}]",
                    link_targets.len(),
                    link_targets.iter().cloned().collect::<Vec<_>>().join(" | ")
                )
            };
            conflicts.push(Conflict {
                kind: ConflictKind::SamePathIncompatible,
                path: path.as_str(),
                other_paths: Vec::new(),
                actions: provs.iter().map(|p| p.action.clone()).collect(),
                detail,
            });
        }

        let rep = chosen.expect("path group is non-empty");
        let (content, content_hash) = if rep.kind == EntryKind::File {
            (rep.content.clone(), rep.content_hash.clone())
        } else {
            (None, None)
        };
        let link_target = if rep.kind == EntryKind::Symlink {
            rep.link_target.clone()
        } else {
            None
        };
        nodes.insert(
            path.clone(),
            PlannedNode {
                path: path.clone(),
                kind: rep.kind,
                actions: provs,
                content,
                content_hash,
                link_target,
            },
        );
    }

    // --- Phase 3: unsafe symlink targets ------------------------------------
    for node in nodes.values() {
        if node.kind != EntryKind::Symlink {
            continue;
        }
        if let Some(raw) = &node.link_target {
            // Raw target was validated by the HTTP layer; normalize again here
            // so the planner stays usable standalone.
            if let Ok(lt) = normalize_target(raw, &node.path) {
                if lt.escapes_root {
                    conflicts.push(Conflict {
                        kind: ConflictKind::UnsafeLinkTarget,
                        path: node.path.as_str(),
                        other_paths: Vec::new(),
                        actions: node.actions.iter().map(|p| p.action.clone()).collect(),
                        detail: format!(
                            "symlink target {} normalizes to {} and escapes the output root",
                            raw, lt.normalized
                        ),
                    });
                }
            }
        }
    }

    // --- Phase 4: file/symlink ancestors block descendant paths -------------
    // Exact-path index of blocking nodes (files and symlinks cannot be dirs).
    let mut blocker_exact: BTreeMap<RelPath, &PlannedNode> = BTreeMap::new();
    let mut blocker_folded: BTreeMap<String, Vec<&PlannedNode>> = BTreeMap::new();
    for n in nodes.values() {
        if matches!(n.kind, EntryKind::File | EntryKind::Symlink) {
            blocker_exact.insert(n.path.clone(), n);
            if !case_sensitive {
                blocker_folded
                    .entry(n.path.folded_str())
                    .or_default()
                    .push(n);
            }
        }
    }

    let mut structural: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut folded_ancestor: BTreeMap<String, Vec<String>> = BTreeMap::new();

    for node in nodes.values() {
        let comps = &node.path.0;
        let folded = node.path.folded();
        for len in 1..comps.len() {
            let prefix = RelPath(comps[..len].to_vec());

            // (a) exact structural ancestor that is a file/symlink
            if let Some(b) = blocker_exact.get(&prefix) {
                structural
                    .entry(b.path.as_str())
                    .or_default()
                    .push(node.path.as_str());
            }

            // (b) ancestor only exists under case folding
            if !case_sensitive {
                let fprefix = folded[..len].join("/");
                if let Some(bs) = blocker_folded.get(&fprefix) {
                    let has_exact = bs.iter().any(|b| b.path.0 == comps[..len]);
                    if !has_exact {
                        // shortest (lexicographically smallest) blocker path
                        let rep = bs
                            .iter()
                            .map(|b| b.path.as_str())
                            .min()
                            .expect("folded group non-empty");
                        folded_ancestor
                            .entry(rep)
                            .or_default()
                            .push(node.path.as_str());
                    }
                }
            }
        }
    }

    for (blocker_path, children) in structural {
        let blocker = blocker_exact
            .get(&RelPath::parse(&blocker_path).unwrap())
            .unwrap();
        let mut child_paths: BTreeSet<String> = children.into_iter().collect();
        child_paths.remove(&blocker_path);
        let children: Vec<String> = child_paths.into_iter().collect();
        if children.is_empty() {
            continue;
        }
        let child_provs: Vec<&[Prov]> = children
            .iter()
            .map(|p| &nodes.get(&RelPath::parse(p).unwrap()).unwrap().actions as &[Prov])
            .collect();
        let mut prov_sets: Vec<&[Prov]> = vec![&blocker.actions];
        prov_sets.extend(child_provs);
        conflicts.push(Conflict {
            kind: ConflictKind::AncestorBlocking,
            detail: format!(
                "{} at this path must be a directory because {} descendant path(s) sit below it",
                blocker.kind.as_str(),
                children.len()
            ),
            path: blocker_path,
            other_paths: children,
            actions: union_actions(&prov_sets),
        });
    }

    for (rep_blocker, children) in folded_ancestor {
        let children: Vec<String> = children
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if children.is_empty() {
            continue;
        }
        let blocker = nodes.get(&RelPath::parse(&rep_blocker).unwrap()).unwrap();
        let child_provs: Vec<&[Prov]> = children
            .iter()
            .map(|p| &nodes.get(&RelPath::parse(p).unwrap()).unwrap().actions as &[Prov])
            .collect();
        let mut prov_sets: Vec<&[Prov]> = vec![&blocker.actions];
        prov_sets.extend(child_provs);
        conflicts.push(Conflict {
            kind: ConflictKind::CaseFoldAncestor,
            detail: format!(
                "{} at this path case-insensitively blocks {} descendant path(s) requiring it to be a directory",
                blocker.kind.as_str(),
                children.len()
            ),
            path: rep_blocker,
            other_paths: children,
            actions: union_actions(&prov_sets),
        });
    }

    // --- Phase 5: case-fold collisions between distinct exact paths ---------
    if !case_sensitive {
        let mut folded_groups: BTreeMap<String, Vec<&PlannedNode>> = BTreeMap::new();
        for n in nodes.values() {
            folded_groups
                .entry(n.path.folded_str())
                .or_default()
                .push(n);
        }
        for (_, group) in folded_groups {
            let exact: BTreeSet<String> = group.iter().map(|n| n.path.as_str()).collect();
            if exact.len() < 2 {
                continue;
            }
            // shortest exact path wins as conflict location; tie -> lexical
            let mut paths: Vec<String> = exact.into_iter().collect();
            paths.sort_by(|a, b| a.len().cmp(&b.len()).then_with(|| a.cmp(b)));
            let head = paths[0].clone();
            let rest: Vec<String> = paths[1..].to_vec();
            let kinds: BTreeSet<EntryKind> = group.iter().map(|n| n.kind).collect();
            let prov_sets: Vec<&[Prov]> = group.iter().map(|n| &n.actions as &[Prov]).collect();
            let detail = if kinds.len() > 1 {
                let names: Vec<&str> = kinds.iter().map(|k| k.as_str()).collect();
                format!(
                    "paths collide when case-folded ({} vs {}); kinds differ: {}",
                    head,
                    rest.join(", "),
                    names.join(", ")
                )
            } else {
                format!(
                    "paths collide when case-folded ({} vs {}) on case-insensitive filesystems",
                    head,
                    rest.join(", ")
                )
            };
            conflicts.push(Conflict {
                kind: ConflictKind::CaseFoldCollision,
                path: head.to_string(),
                other_paths: rest,
                actions: union_actions(&prov_sets),
                detail,
            });
        }
    }

    // --- Phase 6: identical-content sharing groups --------------------------
    let mut by_hash: BTreeMap<String, Vec<&PlannedNode>> = BTreeMap::new();
    for n in nodes.values() {
        if n.kind == EntryKind::File {
            if let Some(h) = &n.content_hash {
                by_hash.entry(h.clone()).or_default().push(n);
            }
        }
    }
    let mut shared: Vec<SharedContent> = Vec::new();
    for (hash, group) in by_hash {
        if group.len() < 2 {
            continue;
        }
        let mut paths: Vec<String> = group.iter().map(|n| n.path.as_str()).collect();
        paths.sort();
        let prov_sets: Vec<&[Prov]> = group.iter().map(|n| &n.actions as &[Prov]).collect();
        let byte_len = group[0].content.as_ref().map(|c| c.len()).unwrap_or(0);
        shared.push(SharedContent {
            content_hash: hash,
            paths,
            actions: union_actions(&prov_sets),
            byte_len,
        });
    }

    // --- Finalize deterministic ordering ------------------------------------
    conflicts.sort_by(|a, b| {
        a.path
            .cmp(&b.path)
            .then_with(|| a.kind.as_str().cmp(b.kind.as_str()))
            .then_with(|| a.other_paths.cmp(&b.other_paths))
    });
    let nodes_out: Vec<PlannedNode> = nodes.into_values().collect();

    Plan {
        nodes: nodes_out,
        conflicts,
        shared,
        action_ids,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn file(action: &str, path: &str, content: &str) -> (String, Vec<OutputSpec>) {
        (
            action.to_string(),
            vec![OutputSpec {
                path: RelPath::parse(path).unwrap(),
                kind: EntryKind::File,
                content: Some(content.as_bytes().to_vec()),
                content_hash: Some(sha256_hex(content.as_bytes())),
                link_target: None,
            }],
        )
    }

    #[test]
    fn file_vs_dir_ancestor_conflict() {
        let mut actions = BTreeMap::new();
        actions.insert("a1".into(), vec![file("a1", "a", "x").1.remove(0)]);
        actions.insert(
            "a2".into(),
            vec![OutputSpec {
                path: RelPath::parse("a/b").unwrap(),
                kind: EntryKind::Dir,
                content: None,
                content_hash: None,
                link_target: None,
            }],
        );
        let plan = build_plan(&actions, false);
        assert_eq!(plan.conflicts.len(), 1);
        assert_eq!(plan.conflicts[0].kind, ConflictKind::AncestorBlocking);
        assert_eq!(plan.conflicts[0].path, "a");
        assert_eq!(plan.conflicts[0].other_paths, vec!["a/b"]);
        assert_eq!(plan.conflicts[0].actions, vec!["a1", "a2"]);
    }

    #[test]
    fn same_path_different_content_conflict() {
        let mut actions = BTreeMap::new();
        actions.insert("a1".into(), vec![file("a1", "out.txt", "one").1.remove(0)]);
        actions.insert("a2".into(), vec![file("a2", "out.txt", "two").1.remove(0)]);
        let plan = build_plan(&actions, false);
        assert_eq!(plan.conflicts.len(), 1);
        assert_eq!(plan.conflicts[0].kind, ConflictKind::SamePathIncompatible);
        assert_eq!(plan.conflicts[0].path, "out.txt");
        assert_eq!(plan.conflicts[0].actions, vec!["a1", "a2"]);
    }

    #[test]
    fn same_content_shared_no_conflict() {
        let mut actions = BTreeMap::new();
        actions.insert("a1".into(), vec![file("a1", "x/a", "payload").1.remove(0)]);
        actions.insert("a2".into(), vec![file("a2", "y/b", "payload").1.remove(0)]);
        let plan = build_plan(&actions, false);
        assert!(plan.conflicts.is_empty(), "{:?}", plan.conflicts);
        assert_eq!(plan.shared.len(), 1);
        assert_eq!(plan.shared[0].paths, vec!["x/a", "y/b"]);
        assert_eq!(plan.shared[0].actions, vec!["a1", "a2"]);
        assert_eq!(plan.shared[0].byte_len, 7);
    }

    #[test]
    fn case_fold_collision_detected() {
        let mut actions = BTreeMap::new();
        actions.insert("a1".into(), vec![file("a1", "Readme", "x").1.remove(0)]);
        actions.insert("a2".into(), vec![file("a2", "README", "y").1.remove(0)]);
        let plan = build_plan(&actions, false);
        assert!(plan
            .conflicts
            .iter()
            .any(|c| c.kind == ConflictKind::CaseFoldCollision
                // equal length -> lexicographically smallest is the location
                && c.path == "README"
                && c.other_paths == vec!["Readme"]));
        // disabled via case_sensitive
        let plan2 = build_plan(&actions, true);
        assert!(!plan2
            .conflicts
            .iter()
            .any(|c| c.kind == ConflictKind::CaseFoldCollision));
    }
}
