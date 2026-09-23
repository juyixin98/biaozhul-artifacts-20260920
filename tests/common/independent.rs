//! Independent reference verifier used by the test suite.
//!
//! Deliberately does NOT call any of the crate's verification or hashing
//! helpers. It re-derives everything straight from the protocol specification
//! using only the SHA-256 primitive, so the generated proofs are checked by
//! genuinely independent logic — not by the same code that produced them.
//!
//! Protocol (as documented in the README):
//! * leaf   H = SHA256(0x00 || u32be(|k|) || k || u32be(|v|) || v)
//! * branch H = SHA256(0x01 || L(32) || R(32))
//! * empty      = SHA256(0x02)
//! * leaves sorted by key; odd level's last node paired with itself;
//!   path side = "right" means the sibling is to the right (node is a left
//!   child), and the leaf's level-0 parity derives bit i of the index.
#![allow(clippy::needless_range_loop, clippy::manual_is_multiple_of)]

use sha2::{Digest, Sha256};

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone)]
pub struct Step {
    pub sibling_hash: String,
    pub side: String, // "left" | "right"
}

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone)]
pub struct EntryJ {
    pub key: String,
    pub value: String,
}

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone)]
pub struct Inclusion {
    pub version: u64,
    pub root: String,
    pub leaf_count: u64,
    pub index: u64,
    pub entry: EntryJ,
    pub path: Vec<Step>,
}

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone)]
pub struct BoundJ {
    pub side: String,
    pub proof: Inclusion,
}

#[derive(serde::Deserialize, serde::Serialize, Debug, Clone)]
pub struct Absence {
    pub version: u64,
    pub root: String,
    pub queried_key: String,
    pub empty_tree: bool,
    #[serde(default)]
    pub bounds: Vec<BoundJ>,
}

#[derive(serde::Serialize, Debug, Clone)]
#[serde(untagged)]
pub enum Response {
    Yes {
        #[serde(rename = "exists")]
        yes: bool,
        key: String,
        value: String,
        proof: Inclusion,
    },
    No {
        #[serde(rename = "exists")]
        no: bool,
        key: String,
        proof: Absence,
    },
}

// serde cannot tag an enum with a *boolean* discriminant via attributes, so
// implement Deserialize by peeking at the `exists` flag, exactly as an
// external JSON consumer would.
impl<'de> serde::Deserialize<'de> for Response {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        match v.get("exists").and_then(|f| f.as_bool()) {
            Some(true) => Ok(Response::Yes {
                yes: true,
                key: serde_json::from_value(v["key"].clone()).map_err(serde::de::Error::custom)?,
                value: serde_json::from_value(v["value"].clone())
                    .map_err(serde::de::Error::custom)?,
                proof: serde_json::from_value(v["proof"].clone())
                    .map_err(serde::de::Error::custom)?,
            }),
            Some(false) => Ok(Response::No {
                no: false,
                key: serde_json::from_value(v["key"].clone()).map_err(serde::de::Error::custom)?,
                proof: serde_json::from_value(v["proof"].clone())
                    .map_err(serde::de::Error::custom)?,
            }),
            other => Err(serde::de::Error::custom(format!(
                "missing/invalid boolean `exists` tag: {other:?}"
            ))),
        }
    }
}

fn hx(s: &str) -> Vec<u8> {
    hex::decode(s.strip_prefix("0x").unwrap_or(s)).expect("hex")
}

fn u32be(n: usize) -> [u8; 4] {
    (n as u32).to_be_bytes()
}

fn leaf_hash(k: &[u8], v: &[u8]) -> [u8; 32] {
    let mut pre = Vec::new();
    pre.push(0x00);
    pre.extend_from_slice(&u32be(k.len()));
    pre.extend_from_slice(k);
    pre.extend_from_slice(&u32be(v.len()));
    pre.extend_from_slice(v);
    let mut o = [0u8; 32];
    o.copy_from_slice(&Sha256::digest(pre));
    o
}

fn branch_hash(l: &[u8; 32], r: &[u8; 32]) -> [u8; 32] {
    let mut o = [0u8; 32];
    o.copy_from_slice(&Sha256::new().chain_update([0x01u8]).chain_update(l).chain_update(r).finalize());
    o
}

fn empty_root() -> [u8; 32] {
    let mut o = [0u8; 32];
    o.copy_from_slice(&Sha256::digest([0x02u8]));
    o
}

fn level_widths(n_leaves: u64, depth: usize) -> Vec<u64> {
    let mut w = vec![n_leaves];
    for _ in 0..depth {
        let last = *w.last().unwrap();
        w.push(last / 2 + last % 2);
    }
    w
}

/// Reference inclusion check; returns `Err(reason)` on any inconsistency.
pub fn check_inclusion(p: &Inclusion, claimed_root_hex: &str) -> Result<(), String> {
    let root = hx(claimed_root_hex);
    if root.len() != 32 {
        return Err("root not 32 bytes".into());
    }
    let root: [u8; 32] = root.try_into().unwrap();
    if hx(&p.root) != root {
        return Err("embedded root != claimed root".into());
    }
    if p.leaf_count == 0 {
        return Err("leaf_count 0".into());
    }
    if p.index >= p.leaf_count {
        return Err("index out of range".into());
    }

    // Expected depth from leaf count alone.
    let mut depth = 0usize;
    let mut w = p.leaf_count;
    while w > 1 {
        w = w / 2 + w % 2;
        depth += 1;
    }
    if p.path.len() != depth {
        return Err(format!("path depth {} != expected {}", p.path.len(), depth));
    }

    let widths = level_widths(p.leaf_count, depth);

    // Index must equal the bits implied by side values: the path node is a
    // right child (bit i = 1) iff its sibling sits on its LEFT.
    let mut derived = 0u64;
    for (i, step) in p.path.iter().enumerate() {
        if step.side == "left" {
            derived |= 1u64 << i;
        } else if step.side != "right" {
            return Err("bad side tag".into());
        }
    }
    if derived != p.index {
        return Err("index inconsistent with sides".into());
    }

    let k = hx(&p.entry.key);
    let v = hx(&p.entry.value);
    let mut cur = leaf_hash(&k, &v);
    let mut slot = p.index;
    for level in 0..depth {
        let width = widths[level];
        let sib_raw = hx(&p.path[level].sibling_hash);
        if sib_raw.len() != 32 {
            return Err("sibling not 32 bytes".into());
        }
        let sib: [u8; 32] = sib_raw.try_into().unwrap();

        cur = match p.path[level].side.as_str() {
            "right" => {
                if slot % 2 != 0 {
                    return Err("side says right but slot is odd".into());
                }
                if slot + 1 == width {
                    // promotion by duplication
                    if sib != cur {
                        return Err("promoted sibling must equal running hash".into());
                    }
                } else if slot + 1 > width {
                    return Err("sibling past width".into());
                }
                branch_hash(&cur, &sib)
            }
            "left" => {
                if slot % 2 != 1 {
                    return Err("side says left but slot is even".into());
                }
                branch_hash(&sib, &cur)
            }
            other => return Err(format!("unknown side {other}")),
        };
        slot /= 2;
    }
    if cur != root {
        return Err("recomputed root mismatch".into());
    }
    Ok(())
}

fn check_bound(b: &BoundJ, root: &[u8; 32]) -> Result<Vec<u8>, String> {
    if hx(&b.proof.root) != *root {
        return Err("bound root differs".into());
    }
    let root_hex = hex::encode(root);
    check_inclusion(&b.proof, &root_hex)?;
    Ok(hx(&b.proof.entry.key))
}

/// Reference non-existence check.
pub fn check_absence(a: &Absence, claimed_root_hex: &str) -> Result<(), String> {
    let rootv = hx(claimed_root_hex);
    if rootv.len() != 32 {
        return Err("root not 32 bytes".into());
    }
    let root: [u8; 32] = rootv.try_into().unwrap();
    if hx(&a.root) != root {
        return Err("embedded root != claimed root".into());
    }
    let q = hx(&a.queried_key);
    let empty = empty_root();

    if a.empty_tree {
        if root != empty {
            return Err("empty_tree flag but root not empty".into());
        }
        if !a.bounds.is_empty() {
            return Err("empty tree with bounds".into());
        }
        return Ok(());
    }
    if root == empty {
        return Err("root empty but flag missing".into());
    }

    match a.bounds.len() {
        1 => {
            let b = &a.bounds[0];
            let nk = check_bound(b, &root)?;
            match b.side.as_str() {
                "left" => {
                    if nk >= q {
                        return Err("left neighbor not smaller".into());
                    }
                    if b.proof.index + 1 != b.proof.leaf_count {
                        return Err("left neighbor not rightmost".into());
                    }
                }
                "right" => {
                    if nk <= q {
                        return Err("right neighbor not greater".into());
                    }
                    if b.proof.index != 0 {
                        return Err("right neighbor not leftmost".into());
                    }
                }
                other => return Err(format!("bad bound side {other}")),
            }
        }
        2 => {
            let (l, r) = if a.bounds[0].side == "left" {
                (&a.bounds[0], &a.bounds[1])
            } else {
                (&a.bounds[1], &a.bounds[0])
            };
            if l.side != "left" || r.side != "right" {
                return Err("bounds must be one left and one right".into());
            }
            let lk = check_bound(l, &root)?;
            let rk = check_bound(r, &root)?;
            if !(lk < q && q < rk) {
                return Err("query not strictly bracketed".into());
            }
            if l.proof.leaf_count != r.proof.leaf_count {
                return Err("leaf_count mismatch across bounds".into());
            }
            if l.proof.index + 1 != r.proof.index {
                return Err("bounds not adjacent".into());
            }
        }
        n => return Err(format!("{n} bounds, expected 1 or 2")),
    }
    Ok(())
}

/// Check either proof embedded in a full API response.
pub fn check_response(resp: &Response, root_hex: &str) -> Result<(), String> {
    match resp {
        Response::Yes { key, value, proof, .. } => {
            if *key != proof.entry.key {
                return Err("response key != proof key".into());
            }
            if *value != proof.entry.value {
                return Err("response value != proof value".into());
            }
            check_inclusion(proof, root_hex)
        }
        Response::No { key, proof, .. } => {
            if *key != proof.queried_key {
                return Err("response key != queried key".into());
            }
            check_absence(proof, root_hex)
        }
    }
}
