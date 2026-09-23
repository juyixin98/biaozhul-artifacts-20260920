//! Independent verifier.
//!
//! This module deliberately does NOT import or call anything in
//! [`crate::core`] / [`crate::store`]. Given only:
//!
//! * the trusted root hash (32 bytes),
//! * the proof JSON document,
//! * the queried key bytes,
//!
//! it re-derives the whole specification from scratch using only the SHA-256
//! compression primitive. A standalone copy of this logic also ships as the
//! `examples/verify.rs` CLI, so verification never needs the service code.
//!
//! ## Why the non-existence proof is sound
//!
//! The verifier cannot see the whole leaf list, only the neighbour leaves the
//! prover hands it. To stop a prover from omitting the real predecessor or
//! successor, the verifier uses the fact that **the bottom-up path encodes the
//! leaf's index**: at level `l`, a `left` sibling means bit `l` of the index is
//! 1; `right`/`promoted` means bit `l` is 0. It then demands:
//!
//! * boundary case before the first leaf: `next.index == 0`;
//! * boundary case after the last leaf: `prev.index == leaf_count - 1`;
//! * interior case: `next.index == prev.index + 1`;
//! * strict key inequalities `prev.key < query < next.key`.
//!
//! If the queried key were actually present at index `j`, no two *consecutive*
//! indices can have keys strictly straddling it (the honest root places the
//! queried leaf itself between them), so the check fails. Absence is never
//! represented by an empty value: existence always carries a full leaf
//! preimage, and an empty-byte value verifies identically to any other value.

use sha2::{Digest, Sha256};

/// Verification failure. The string is a machine/human-readable reason.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyError(pub String);

impl VerifyError {
    fn new(msg: impl Into<String>) -> Self {
        VerifyError(msg.into())
    }
}

impl std::fmt::Display for VerifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for VerifyError {}

type VResult<T> = Result<T, VerifyError>;

// ---- spec primitives, independently re-derived here ----------------------

const LEAF: u8 = 0x00;
const INNER: u8 = 0x01;
const COMMIT: u8 = 0x02;

fn h(data: &[u8]) -> [u8; 32] {
    let mut s = Sha256::new();
    s.update(data);
    s.finalize().into()
}

fn leaf_hash(key: &[u8], val: &[u8]) -> [u8; 32] {
    let mut p = Vec::with_capacity(1 + 4 + key.len() + 4 + val.len());
    p.push(LEAF);
    p.extend_from_slice(&(key.len() as u32).to_be_bytes());
    p.extend_from_slice(key);
    p.extend_from_slice(&(val.len() as u32).to_be_bytes());
    p.extend_from_slice(val);
    h(&p)
}

fn inner_hash(l: &[u8; 32], r: &[u8; 32]) -> [u8; 32] {
    let mut p = [0u8; 65];
    p[0] = INNER;
    p[1..33].copy_from_slice(l);
    p[33..65].copy_from_slice(r);
    h(&p)
}

fn commit_hash(version: u64, top: &[u8; 32], n: u64, height: u32) -> [u8; 32] {
    let mut p = Vec::with_capacity(53);
    p.push(COMMIT);
    p.extend_from_slice(&version.to_be_bytes());
    p.extend_from_slice(top);
    p.extend_from_slice(&n.to_be_bytes());
    p.extend_from_slice(&height.to_be_bytes());
    h(&p)
}

fn height_of(n: u64) -> u32 {
    let (mut hh, mut c) = (0u32, n);
    while c > 1 {
        c = c.div_ceil(2);
        hh += 1;
    }
    hh
}

// ---- minimal JSON model (serde_json is already a dependency) --------------

#[derive(Debug, Clone)]
struct Step {
    side: String,
    hash: Option<[u8; 32]>,
}

#[derive(Debug, Clone)]
struct Branch {
    index: u64,
    key: Vec<u8>,
    value: Vec<u8>,
    path: Vec<Step>,
}

#[derive(Debug, Clone)]
enum Proof {
    Existence {
        version: u64,
        n: u64,
        height: u32,
        branch: Branch,
    },
    NonExistence {
        version: u64,
        n: u64,
        height: u32,
        prev: Option<Branch>,
        next: Option<Branch>,
    },
}

fn hex32(v: &serde_json::Value, field: &str) -> VResult<[u8; 32]> {
    let s = v
        .get(field)
        .and_then(|x| x.as_str())
        .ok_or_else(|| VerifyError::new(format!("missing string field `{field}`")))?;
    let bytes = decode_hex(s).map_err(|e| VerifyError::new(format!("field `{field}`: {e}")))?;
    bytes
        .try_into()
        .map_err(|_| VerifyError::new(format!("field `{field}` must be exactly 32 bytes")))
}

fn hex_bytes(v: &serde_json::Value, field: &str) -> VResult<Vec<u8>> {
    let s = v
        .get(field)
        .and_then(|x| x.as_str())
        .ok_or_else(|| VerifyError::new(format!("missing string field `{field}`")))?;
    decode_hex(s).map_err(|e| VerifyError::new(format!("field `{field}`: {e}")))
}

fn decode_hex(s: &str) -> Result<Vec<u8>, String> {
    if !s.len().is_multiple_of(2) {
        return Err("odd-length hex".into());
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        let hi = hex_nibble(b[i]).map_err(|e| format!("{e} at position {i}"))?;
        let lo = hex_nibble(b[i + 1]).map_err(|e| format!("{e} at position {}", i + 1))?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8, String> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        other => Err(format!("invalid hex digit 0x{other:02x}")),
    }
}

fn u64_field(v: &serde_json::Value, f: &str) -> VResult<u64> {
    v.get(f)
        .and_then(|x| x.as_u64())
        .ok_or_else(|| VerifyError::new(format!("missing integer field `{f}`")))
}

fn u32_field(v: &serde_json::Value, f: &str) -> VResult<u32> {
    u64_field(v, f).and_then(|x| {
        u32::try_from(x).map_err(|_| VerifyError::new(format!("field `{f}` out of u32 range")))
    })
}

fn parse_branch(v: &serde_json::Value) -> VResult<Branch> {
    let index = u64_field(v, "index")?;
    let key = hex_bytes(v, "key_hex")?;
    let value = hex_bytes(v, "value_hex")?;
    let arr = v
        .get("path")
        .and_then(|x| x.as_array())
        .ok_or_else(|| VerifyError::new("branch missing `path` array"))?;
    let mut path = Vec::with_capacity(arr.len());
    for step in arr {
        let side = step
            .get("side")
            .and_then(|x| x.as_str())
            .ok_or_else(|| VerifyError::new("path step missing `side`"))?
            .to_string();
        if !matches!(side.as_str(), "left" | "right" | "promoted") {
            return Err(VerifyError::new(format!("unknown path side `{side}`")));
        }
        let hash = match step.get("hash") {
            None | Some(serde_json::Value::Null) => None,
            Some(_) => Some(hex32(step, "hash")?),
        };
        path.push(Step { side, hash });
    }
    Ok(Branch {
        index,
        key,
        value,
        path,
    })
}

fn parse_proof(doc: &serde_json::Value) -> VProof {
    let version = u64_field(doc, "version")?;
    let n = u64_field(doc, "leaf_count")?;
    let height = u32_field(doc, "height")?;
    let kind = doc
        .get("kind")
        .and_then(|x| x.as_str())
        .ok_or_else(|| VerifyError::new("missing `kind`"))?;
    match kind {
        "existence" => Ok(Proof::Existence {
            version,
            n,
            height,
            branch: parse_branch(doc)?,
        }),
        "non_existence" => Ok(Proof::NonExistence {
            version,
            n,
            height,
            prev: match doc.get("prev") {
                None | Some(serde_json::Value::Null) => None,
                Some(v) => Some(parse_branch(v)?),
            },
            next: match doc.get("next") {
                None | Some(serde_json::Value::Null) => None,
                Some(v) => Some(parse_branch(v)?),
            },
        }),
        other => Err(VerifyError::new(format!("unknown proof kind `{other}`"))),
    }
}

type VProof = VResult<Proof>;

// ---- core replay -----------------------------------------------------------

/// Replay one branch bottom-up. Returns `(reconstructed_index, top)`.
///
/// Every structural rule (sibling ordering, promotion of an odd trailing node,
/// level sizes) is checked against `leaf_count` — never taken on trust.
fn replay(b: &Branch, n: u64, height: u32) -> VResult<(u64, [u8; 32])> {
    if n == 0 {
        return Err(VerifyError::new(
            "branch supplied for an empty (0-leaf) tree",
        ));
    }
    if b.index >= n {
        return Err(VerifyError::new(format!(
            "branch index {} out of range (leaf_count={n})",
            b.index
        )));
    }
    if b.path.len() != height as usize {
        return Err(VerifyError::new(format!(
            "path has {} steps but tree height is {height}",
            b.path.len()
        )));
    }
    if height != height_of(n) {
        return Err(VerifyError::new(format!(
            "commit height {height} inconsistent with leaf_count {n} (expected {})",
            height_of(n)
        )));
    }

    let mut cur = leaf_hash(&b.key, &b.value);
    let mut pos = b.index;
    let mut count = n;
    let mut reconstructed_index: u64 = 0;

    for (level, step) in b.path.iter().enumerate() {
        let bit: u64 = match step.side.as_str() {
            "left" => {
                // current node is the right child: index must be odd
                if pos.is_multiple_of(2) {
                    return Err(VerifyError::new(format!(
                        "level {level}: `left` sibling but position {pos} is even"
                    )));
                }
                let sib = step.hash.ok_or_else(|| {
                    VerifyError::new(format!("level {level}: `left` step missing hash"))
                })?;
                cur = inner_hash(&sib, &cur);
                1
            }
            "right" => {
                // current node is the left child with a real right sibling
                if !pos.is_multiple_of(2) {
                    return Err(VerifyError::new(format!(
                        "level {level}: `right` sibling but position {pos} is odd"
                    )));
                }
                if pos + 1 >= count {
                    return Err(VerifyError::new(format!(
                        "level {level}: `right` sibling at last position {pos} of {count} (should be `promoted`)"
                    )));
                }
                let sib = step.hash.ok_or_else(|| {
                    VerifyError::new(format!("level {level}: `right` step missing hash"))
                })?;
                cur = inner_hash(&cur, &sib);
                0
            }
            "promoted" => {
                // lone odd trailing node, carried up unchanged, hash MUST be absent
                if !pos.is_multiple_of(2) || pos + 1 != count {
                    return Err(VerifyError::new(format!(
                        "level {level}: `promoted` only valid for even last position (pos={pos}, count={count})"
                    )));
                }
                if step.hash.is_some() {
                    return Err(VerifyError::new(format!(
                        "level {level}: `promoted` step must not carry a sibling hash"
                    )));
                }
                0
            }
            other => return Err(VerifyError::new(format!("level {level}: bad side {other}"))),
        };
        reconstructed_index |= bit << level;
        pos /= 2;
        count = count.div_ceil(2);
    }

    if count != 1 || pos != 0 {
        return Err(VerifyError::new(
            "path did not converge to a single root node",
        ));
    }
    if reconstructed_index != b.index {
        return Err(VerifyError::new(format!(
            "path direction bits reconstruct index {reconstructed_index}, proof claims index {}",
            b.index
        )));
    }
    Ok((reconstructed_index, cur))
}

/// Verify a proof JSON document against a trusted root.
///
/// * `proof_json` — the exact document returned by `POST /v1/proofs`
/// * `expected_root_hex` — the trusted root hash for the proof's version
/// * `query_key` — the key the client asked about (binds the proof to the
///   client's query, independent of fields embedded in the document)
pub fn verify_proof_slice(
    proof_json: &[u8],
    expected_root: &[u8; 32],
    query_key: &[u8],
) -> VResult<Verified> {
    let doc: serde_json::Value = serde_json::from_slice(proof_json)
        .map_err(|e| VerifyError::new(format!("proof is not valid JSON: {e}")))?;
    verify_value(&doc, expected_root, query_key)
}

/// Outcome of a successful verification.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Verified {
    /// Key exists; value bytes are attached (may be empty — present-empty).
    Present(Vec<u8>),
    /// Key is absent. Carries the bounding indices/keys actually verified.
    Absent(AbsenceBound),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AbsenceBound {
    pub prev: Option<(u64, Vec<u8>)>,
    pub next: Option<(u64, Vec<u8>)>,
}

fn check_root_binding(
    doc: &serde_json::Value,
    version: u64,
    n: u64,
    height: u32,
    top: &[u8; 32],
    expected_root: &[u8; 32],
) -> VResult<()> {
    let claimed_top = hex32(doc, "top")?;
    if &claimed_top != top {
        return Err(VerifyError::new(
            "reconstructed tree top does not match proof `top`",
        ));
    }
    let claimed_root = hex32(doc, "root")?;
    if &claimed_root != expected_root {
        return Err(VerifyError::new(
            "proof `root` does not match the trusted root supplied by the client",
        ));
    }
    let recomputed = commit_hash(version, top, n, height);
    if &recomputed != expected_root {
        return Err(VerifyError::new(
            "reconstructed commit hash does not match the trusted root",
        ));
    }
    Ok(())
}

fn verify_value(
    doc: &serde_json::Value,
    expected_root: &[u8; 32],
    query_key: &[u8],
) -> VResult<Verified> {
    let proof = parse_proof(doc)?;

    // Bind embedded query field (if present) to the caller-supplied key.
    if let Some(q) = doc.get("query_key_hex").and_then(|x| x.as_str()) {
        let embedded = decode_hex(q).map_err(VerifyError::new)?;
        if embedded != query_key {
            return Err(VerifyError::new(
                "proof `query_key_hex` does not match the caller-supplied query key",
            ));
        }
    }

    match proof {
        Proof::Existence {
            version,
            n,
            height,
            branch,
        } => {
            if branch.key != query_key {
                return Err(VerifyError::new(
                    "existence proof leaf key does not match queried key",
                ));
            }
            if n == 0 {
                return Err(VerifyError::new("existence proof against empty tree"));
            }
            let (idx, top) = replay(&branch, n, height)?;
            let _ = idx;
            check_root_binding(doc, version, n, height, &top, expected_root)?;
            Ok(Verified::Present(branch.value))
        }
        Proof::NonExistence {
            version,
            n,
            height,
            prev,
            next,
        } => {
            // Empty-tree case: no neighbours, commit must pin top = zero hash.
            if n == 0 {
                if prev.is_some() || next.is_some() {
                    return Err(VerifyError::new(
                        "empty-tree absence proof must carry no neighbours",
                    ));
                }
                if height != 0 {
                    return Err(VerifyError::new("empty tree height must be 0"));
                }
                let top = [0u8; 32];
                check_root_binding(doc, version, n, height, &top, expected_root)?;
                return Ok(Verified::Absent(AbsenceBound {
                    prev: None,
                    next: None,
                }));
            }

            if prev.is_none() && next.is_none() {
                return Err(VerifyError::new(
                    "non-empty tree absence proof needs at least one neighbour",
                ));
            }

            let mut bound = AbsenceBound {
                prev: None,
                next: None,
            };
            let mut top: Option<[u8; 32]> = None;

            if let Some(p) = &prev {
                if !(p.key.as_slice() < query_key) {
                    return Err(VerifyError::new("prev key is not strictly less than queried key".to_string()));
                }
                let (idx, t) = replay(p, n, height)?;
                if idx != p.index {
                    return Err(VerifyError::new("prev index replay mismatch"));
                }
                top.get_or_insert(t);
                if top.unwrap() != t {
                    return Err(VerifyError::new("prev branch replays to a different top"));
                }
                bound.prev = Some((p.index, p.key.clone()));
            }
            if let Some(q) = &next {
                if !(query_key < q.key.as_slice()) {
                    return Err(VerifyError::new(
                        "next key is not strictly greater than queried key",
                    ));
                }
                let (idx, t) = replay(q, n, height)?;
                if idx != q.index {
                    return Err(VerifyError::new("next index replay mismatch"));
                }
                if let Some(tt) = top {
                    if tt != t {
                        return Err(VerifyError::new(
                            "next branch replays to a different top than prev branch",
                        ));
                    }
                } else {
                    top = Some(t);
                }
                bound.next = Some((q.index, q.key.clone()));
            }

            // Index adjacency / boundary checks — the actual non-membership test.
            match (&bound.prev, &bound.next) {
                (Some((pi, _)), Some((qi, _))) => {
                    if *qi != pi + 1 {
                        return Err(VerifyError::new(format!(
                            "non-adjacent neighbours: prev index {pi}, next index {qi} (must differ by 1)"
                        )));
                    }
                }
                (None, Some((qi, _))) => {
                    if *qi != 0 {
                        return Err(VerifyError::new(format!(
                            "missing prev but next index is {qi}, not 0 — a smaller leaf exists and is withheld"
                        )));
                    }
                }
                (Some((pi, _)), None) => {
                    if *pi != n - 1 {
                        return Err(VerifyError::new(format!(
                            "missing next but prev index {pi} is not last index {} — a larger leaf is withheld",
                            n - 1
                        )));
                    }
                }
                (None, None) => unreachable!(),
            }

            let top = top.ok_or_else(|| VerifyError::new("no branch replayed"))?;
            check_root_binding(doc, version, n, height, &top, expected_root)?;
            Ok(Verified::Absent(bound))
        }
    }
}
