//! Property-style round-trip tests with a deterministic PRNG (no `rand`
//! dependency): random trees are built, validated, and every node is
/// resolved by its path; blob bytes are compared. Multi-level TOC fanouts
/// are exercised explicitly.
mod common;

use common::*;
use ifix::format::Limits;
use serde_json::{json, Value};
use std::io::Cursor;

/// Tiny deterministic xorshift PRNG.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next() % n) as usize
    }
}

fn random_name(rng: &mut Rng, n: usize) -> String {
    const CHARS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789";
    (0..n)
        .map(|_| CHARS[rng.below(CHARS.len() as u64)] as char)
        .collect()
}

/// A generated file's full path and expected blob bytes (None = empty file).
struct GenNode {
    path: String,
    blob: Option<Vec<u8>>,
}

fn random_tree(rng: &mut Rng, max_depth: u32, max_files: usize) -> (Value, Vec<GenNode>) {
    let mut files = Vec::new();
    let root = gen_dir(rng, String::new(), 0, max_depth, &mut files, max_files);
    (root, files)
}

fn gen_dir(
    rng: &mut Rng,
    path: String,
    depth: u32,
    max_depth: u32,
    files: &mut Vec<GenNode>,
    max_files: usize,
) -> Value {
    let child_dirs = if depth < max_depth { rng.below(3) } else { 0 };
    let child_files = rng.below(4);
    let mut children = Vec::new();
    let mut used = std::collections::HashSet::new();

    for _ in 0..child_dirs {
        if files.len() >= max_files {
            break;
        }
        let mut name = random_name(rng, 4);
        while !used.insert(name.clone()) {
            name.push(rng.below(26) as u8 as char);
        }
        let child_path = format!("{path}/{name}");
        children.push(gen_dir(
            rng,
            child_path,
            depth + 1,
            max_depth,
            files,
            max_files,
        ));
    }
    for _ in 0..child_files {
        if files.len() >= max_files {
            break;
        }
        let mut name = format!("{}.txt", random_name(rng, 4));
        while !used.insert(name.clone()) {
            name = format!("{}.txt", random_name(rng, 5));
        }
        let child_path = format!("{path}/{name}");
        // Mostly non-empty, sometimes empty blobs.
        let (node, blob) = if rng.below(5) == 0 {
            (json!({"name": name, "type": "file"}), None)
        } else {
            let len = rng.below(200);
            let bytes: Vec<u8> = (0..len).map(|_| rng.next() as u8).collect();
            let node = json!({
                "name": name, "type": "file",
                "content_b64": ifix::base64::encode(&bytes)
            });
            (node, Some(bytes))
        };
        files.push(GenNode {
            path: child_path.clone(),
            blob: blob.clone(),
        });
        children.push(node);
    }

    json!({"name": if path.is_empty() { "" } else { path.rsplit('/').next().unwrap() },
           "type": "dir", "children": children})
}

#[test]
fn randomized_roundtrips_across_fanouts() {
    for seed in [1u64, 42, 99, 7777, 31337] {
        for log2_page in [5u8, 6, 8, 12] {
            let mut rng = Rng(seed.wrapping_mul(1_000_003).wrapping_add(log2_page as u64));
            let (root, expected) = random_tree(&mut rng, 4, 120);
            let bytes = build_from_value_fanout(root, log2_page);
            let idx = open_mem(bytes);

            // The root resolves and lists everything.
            assert_eq!(idx.lookup("/").unwrap().id, 0);

            // Every generated file is reachable by path and bytes match.
            for g in &expected {
                let meta = idx
                    .lookup(&g.path)
                    .unwrap_or_else(|e| panic!("seed {seed}: {}", e));
                assert!(!meta.is_dir, "{} should be a file", g.path);
                let mut out = Cursor::new(Vec::new());
                idx.read_blob(&g.path, &mut out).unwrap();
                match &g.blob {
                    None => {
                        assert_eq!(meta.size, 0, "{} expected empty", g.path);
                        assert!(out.get_ref().is_empty());
                    }
                    Some(want) => assert_eq!(out.get_ref(), want, "blob mismatch at {}", g.path),
                }
            }
        }
    }
}

#[test]
fn wide_tree_forces_deep_toc_and_all_paths_resolve() {
    // 3000 files under one root: fanout 32 -> leaf 94 pages -> L1 3 pages ->
    // root, i.e. a 3-level TOC.
    let mut rng = Rng(0xABCDEF);
    let mut children = Vec::new();
    let mut names = std::collections::BTreeSet::new();
    while names.len() < 3000 {
        names.insert(format!("{:08}.bin", rng.next()));
    }
    let mut expected = std::collections::BTreeMap::new();
    for (i, name) in names.iter().enumerate() {
        let bytes = vec![(i % 251) as u8; 1 + i % 37];
        expected.insert(format!("/{name}"), bytes.clone());
        children.push(json!({
            "name": name, "type": "file",
            "content_b64": ifix::base64::encode(&bytes)
        }));
    }
    // 3000 files + root = 3001 nodes at fanout 32:
    // leaf 3001 -> 94 pages -> L1 94 -> 3 -> L2 3 -> 1 -> L3(root): 4 levels.
    let root = json!({"name": "", "type": "dir", "children": children});
    let bytes = build_from_value_fanout(root, 5);
    let idx = open_mem(bytes);
    assert_eq!(idx.toc_height(), 4, "expected a 4-level TOC");
    for (path, want) in &expected {
        let mut out = Cursor::new(Vec::new());
        idx.read_blob(path, &mut out).unwrap();
        assert_eq!(out.get_ref(), want);
    }
}

#[test]
fn building_rejects_duplicate_sibling_names() {
    let root = json!({
        "name": "", "type": "dir",
        "children": [
            {"name": "a", "type": "file"},
            {"name": "a", "type": "file"}
        ]
    });
    let req = json!({"root": root});
    let text = serde_json::to_string(&req).unwrap();
    let err = ifix::control::build_to_memory(&text, &Limits::default()).unwrap_err();
    assert_eq!(err.code(), "JSON");
}
