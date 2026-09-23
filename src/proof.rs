//! Proof generation for a versioned snapshot.
//!
//! Produces exactly the JSON document shape that [`crate::verifier`] consumes:
//!
//! ```json
//! {
//!   "kind": "existence" | "non_existence",
//!   "version": 3, "root": "..", "top": "..",
//!   "leaf_count": 4, "height": 2, "query_key_hex": "..",
//!   "index": 2, "key_hex": "..", "value_hex": "..",      // existence
//!   "path": [ {"side": "left|right|promoted", "hash": "..hex or null"} ],
//!   "prev": {branch}|null, "next": {branch}              // non_existence
//! }
//! ```

use crate::core::{prove_index, BuiltTree, PathEntry};
use crate::error::{AppError, AppResult};
use crate::store::Store;
use serde_json::{json, Value};

fn step_json(e: &PathEntry) -> Value {
    match e {
        PathEntry::Left(h) => json!({ "side": "left", "hash": hex::encode(h) }),
        PathEntry::Right(h) => json!({ "side": "right", "hash": hex::encode(h) }),
        PathEntry::Promoted => json!({ "side": "promoted", "hash": Value::Null }),
    }
}

fn branch_json(index: u64, tree: &BuiltTree) -> Value {
    let (k, v) = &tree.leaves[index as usize];
    let path: Vec<Value> = prove_index(tree, index).iter().map(step_json).collect();
    json!({
        "index": index,
        "key_hex": hex::encode(k),
        "value_hex": hex::encode(v),
        "path": path,
    })
}

fn envelope(
    kind: &str,
    version: u64,
    root: [u8; 32],
    tree: &BuiltTree,
    query: &[u8],
) -> serde_json::Map<String, Value> {
    let mut m = serde_json::Map::new();
    m.insert("kind".into(), json!(kind));
    m.insert("version".into(), json!(version));
    m.insert("root".into(), json!(hex::encode(root)));
    m.insert("top".into(), json!(hex::encode(tree.top)));
    m.insert("leaf_count".into(), json!(tree.leaf_count));
    m.insert("height".into(), json!(tree.height));
    m.insert("query_key_hex".into(), json!(hex::encode(query)));
    m
}

/// Generate an existence or non-existence proof at `version` (current if
/// `None`) for `key`.
pub fn generate(store: &Store, version: Option<u64>, key: &[u8]) -> AppResult<Value> {
    if key.is_empty() {
        return Err(AppError::bad_request("query key must be non-empty"));
    }
    // resolve_version semantics (404 on unknown / no versions) via root_info
    let info = store.root_info(version)?;
    let (mfst, tree) = store.tree_at(info.version)?;
    debug_assert_eq!(mfst.root, info.root);

    // Binary search over the sorted manifest leaves.
    match tree.leaves.binary_search_by(|(k, _)| k.as_slice().cmp(key)) {
        Ok(idx) => {
            let mut env = envelope("existence", info.version, info.root, &tree, key);
            let branch = branch_json(idx as u64, &tree);
            for (k, v) in branch.as_object().unwrap() {
                env.insert(k.clone(), v.clone());
            }
            Ok(Value::Object(env))
        }
        Err(insert_at) => {
            let mut env = envelope("non_existence", info.version, info.root, &tree, key);
            if tree.leaf_count == 0 {
                env.insert("prev".into(), Value::Null);
                env.insert("next".into(), Value::Null);
                return Ok(Value::Object(env));
            }
            let prev_idx = if insert_at == 0 {
                None
            } else {
                Some(insert_at - 1)
            };
            let next_idx = if insert_at as u64 == tree.leaf_count {
                None
            } else {
                Some(insert_at)
            };
            env.insert(
                "prev".into(),
                match prev_idx {
                    Some(i) => branch_json(i as u64, &tree),
                    None => Value::Null,
                },
            );
            env.insert(
                "next".into(),
                match next_idx {
                    Some(i) => branch_json(i as u64, &tree),
                    None => Value::Null,
                },
            );
            Ok(Value::Object(env))
        }
    }
}
