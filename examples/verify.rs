//! Standalone proof verifier — a self-contained re-implementation.
//!
//! This file deliberately does NOT use any module from the
//! `merkle_proof_service` crate (not even its `verifier`). It takes a proof
//! document, a trusted root and a queried key, and re-derives everything with
//! only SHA-256. That is exactly the situation of a remote verifier: the
//! service is not trusted beyond the root the client already pinned.
//!
//! Usage:
//!
//! ```text
//! cargo run --release --example verify -- <proof.json|- > <root_hex> <key_hex>
//! ```
//!
//! Exit code 0 = verified (prints PRESENT/ABSENT); 1 = verification failed
//! with a precise reason.

use sha2::{Digest, Sha256};

fn sha(b: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(b);
    h.finalize().into()
}

fn leaf_hash(k: &[u8], v: &[u8]) -> [u8; 32] {
    let mut p = Vec::new();
    p.push(0x00);
    p.extend_from_slice(&(k.len() as u32).to_be_bytes());
    p.extend_from_slice(k);
    p.extend_from_slice(&(v.len() as u32).to_be_bytes());
    p.extend_from_slice(v);
    sha(&p)
}

fn inner(l: &[u8; 32], r: &[u8; 32]) -> [u8; 32] {
    let mut p = Vec::with_capacity(65);
    p.push(0x01);
    p.extend_from_slice(l);
    p.extend_from_slice(r);
    sha(&p)
}

fn commit(version: u64, top: &[u8; 32], n: u64, height: u32) -> [u8; 32] {
    let mut p = Vec::with_capacity(53);
    p.push(0x02);
    p.extend_from_slice(&version.to_be_bytes());
    p.extend_from_slice(top);
    p.extend_from_slice(&n.to_be_bytes());
    p.extend_from_slice(&height.to_be_bytes());
    sha(&p)
}

fn hexdec(s: &str) -> Result<Vec<u8>, String> {
    if !s.len().is_multiple_of(2) {
        return Err("odd hex length".into());
    }
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len() / 2);
    let nib = |c: u8| -> Result<u8, String> {
        Ok(match c {
            b'0'..=b'9' => c - b'0',
            b'a'..=b'f' => c - b'a' + 10,
            b'A'..=b'F' => c - b'A' + 10,
            _ => return Err(format!("bad hex byte {c:#x}")),
        })
    };
    for i in (0..b.len()).step_by(2) {
        out.push((nib(b[i])? << 4) | nib(b[i + 1])?);
    }
    Ok(out)
}

fn height_of(mut n: u64) -> u32 {
    let mut h = 0;
    while n > 1 {
        n = n.div_ceil(2);
        h += 1;
    }
    h
}

struct Branch {
    index: u64,
    key: Vec<u8>,
    value: Vec<u8>,
    path: Vec<(String, Option<[u8; 32]>)>,
}

fn parse_branch(v: &serde_json::Value) -> Result<Branch, String> {
    let index = v
        .get("index")
        .and_then(|x| x.as_u64())
        .ok_or("branch: missing index")?;
    let key = hexdec(
        v.get("key_hex")
            .and_then(|x| x.as_str())
            .ok_or("branch: missing key_hex")?,
    )?;
    let value = hexdec(
        v.get("value_hex")
            .and_then(|x| x.as_str())
            .ok_or("branch: missing value_hex")?,
    )?;
    let mut path = Vec::new();
    for s in v
        .get("path")
        .and_then(|x| x.as_array())
        .ok_or("branch: missing path")?
    {
        let side = s
            .get("side")
            .and_then(|x| x.as_str())
            .ok_or("step: missing side")?
            .to_string();
        let hash = match s.get("hash") {
            Some(serde_json::Value::String(h)) => Some(
                hexdec(h)?
                    .try_into()
                    .map_err(|_| "step hash != 32B".to_string())?,
            ),
            _ => None,
        };
        path.push((side, hash));
    }
    Ok(Branch {
        index,
        key,
        value,
        path,
    })
}

fn replay(b: &Branch, n: u64, height: u32) -> Result<(u64, [u8; 32]), String> {
    if n == 0 {
        return Err("branch for empty tree".into());
    }
    if b.index >= n {
        return Err(format!("index {} >= leaf_count {n}", b.index));
    }
    if b.path.len() != height as usize || height != height_of(n) {
        return Err(format!(
            "path length {} inconsistent with height {height}/n {n}",
            b.path.len()
        ));
    }
    let mut cur = leaf_hash(&b.key, &b.value);
    let (mut pos, mut count, mut idx) = (b.index, n, 0u64);
    for (level, (side, sib)) in b.path.iter().enumerate() {
        let bit = match side.as_str() {
            "left" => {
                if pos % 2 == 0 {
                    return Err(format!("level {level}: left sibling at even pos"));
                }
                let s = sib.ok_or("left step missing hash")?;
                cur = inner(&s, &cur);
                1
            }
            "right" => {
                if pos % 2 != 0 || pos + 1 >= count {
                    return Err(format!("level {level}: invalid right sibling"));
                }
                let s = sib.ok_or("right step missing hash")?;
                cur = inner(&cur, &s);
                0
            }
            "promoted" => {
                if pos % 2 != 0 || pos + 1 != count || sib.is_some() {
                    return Err(format!("level {level}: invalid promotion"));
                }
                0
            }
            other => return Err(format!("unknown side {other}")),
        };
        idx |= bit << level;
        pos /= 2;
        count = count.div_ceil(2);
    }
    if count != 1 || pos != 0 || idx != b.index {
        return Err("path does not converge to the claimed index".into());
    }
    Ok((idx, cur))
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 3 {
        eprintln!("usage: verify <proof.json|-> <root_hex> <query_key_hex>");
        std::process::exit(2);
    }
    let raw = if args[0] == "-" {
        use std::io::Read;
        let mut buf = Vec::new();
        std::io::stdin().read_to_end(&mut buf).expect("read stdin");
        buf
    } else {
        std::fs::read(&args[0]).expect("read proof file")
    };
    let doc: serde_json::Value = serde_json::from_slice(&raw).expect("proof JSON parse");
    let trusted: [u8; 32] = hexdec(&args[1])
        .expect("root hex")
        .try_into()
        .expect("root 32 bytes");
    let query = hexdec(&args[2]).expect("query key hex");

    let result = (|| -> Result<String, String> {
        let version = doc
            .get("version")
            .and_then(|x| x.as_u64())
            .ok_or("missing version")?;
        let n = doc
            .get("leaf_count")
            .and_then(|x| x.as_u64())
            .ok_or("missing leaf_count")?;
        let height = doc
            .get("height")
            .and_then(|x| x.as_u64())
            .ok_or("missing height")? as u32;
        let top_field: [u8; 32] = hexdec(
            doc.get("top")
                .and_then(|x| x.as_str())
                .ok_or("missing top")?,
        )?
        .try_into()
        .map_err(|_| "top != 32B".to_string())?;
        let root_field: [u8; 32] = hexdec(
            doc.get("root")
                .and_then(|x| x.as_str())
                .ok_or("missing root")?,
        )?
        .try_into()
        .map_err(|_| "root != 32B".to_string())?;
        if root_field != trusted {
            return Err("proof root != trusted root supplied by caller".into());
        }

        match doc.get("kind").and_then(|x| x.as_str()) {
            Some("existence") => {
                let b = parse_branch(&doc)?;
                if b.key != query {
                    return Err("leaf key != queried key".into());
                }
                let (_, top) = replay(&b, n, height)?;
                if top != top_field {
                    return Err("reconstructed top != proof top".into());
                }
                if commit(version, &top, n, height) != trusted {
                    return Err("commit hash mismatch".into());
                }
                Ok(format!(
                    "VERIFIED PRESENT version={version} value_hex={}",
                    hex::encode(&b.value)
                ))
            }
            Some("non_existence") => {
                let mut bound = String::new();
                let top = if n == 0 {
                    [0u8; 32]
                } else {
                    let prev = doc.get("prev").filter(|x| !x.is_null());
                    let next = doc.get("next").filter(|x| !x.is_null());
                    let mut found: Option<[u8; 32]> = None;
                    if let Some(p) = prev {
                        let b = parse_branch(p)?;
                        if !(b.key.as_slice() < query.as_slice()) {
                            return Err("prev key not < query".into());
                        }
                        let (idx, t) = replay(&b, n, height)?;
                        found = Some(t);
                        bound = format!("prev(idx={idx})");
                        match next {
                            Some(q) => {
                                let bq = parse_branch(q)?;
                                if !(query.as_slice() < bq.key.as_slice()) {
                                    return Err("next key not > query".into());
                                }
                                let (jdx, t2) = replay(&bq, n, height)?;
                                if t != t2 {
                                    return Err("branches replay to different tops".into());
                                }
                                if jdx != idx + 1 {
                                    return Err(format!("non-adjacent neighbours {idx},{jdx}"));
                                }
                                bound.push_str(&format!(" next(idx={jdx})"));
                            }
                            None => {
                                if idx != n - 1 {
                                    return Err(format!(
                                        "withheld successor: prev idx {idx} != last {}",
                                        n - 1
                                    ));
                                }
                            }
                        }
                    }
                    if let Some(q) = next {
                        let bq = parse_branch(q)?;
                        if !(query.as_slice() < bq.key.as_slice()) {
                            return Err("next key not > query".into());
                        }
                        let (jdx, t2) = replay(&bq, n, height)?;
                        if found.is_none() {
                            if jdx != 0 {
                                return Err(format!("withheld predecessor: next idx {jdx} != 0"));
                            }
                            found = Some(t2);
                            bound = format!("next(idx={jdx})");
                        }
                    }
                    found.ok_or("empty branches")?
                };
                if top != top_field {
                    return Err("reconstructed top != proof top".into());
                }
                if commit(version, &top, n, height) != trusted {
                    return Err("commit hash mismatch".into());
                }
                Ok(format!("VERIFIED ABSENT version={version} ({bound})"))
            }
            k => Err(format!("bad kind {k:?}")),
        }
    })();

    match result {
        Ok(msg) => {
            println!("{msg}");
        }
        Err(e) => {
            eprintln!("REJECTED: {e}");
            std::process::exit(1);
        }
    }
}
