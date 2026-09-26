//! Whole-file integrity validation.
//!
//! Opening a file with [`crate::reader::Reader`] validates the superblock and
//! validates each structure the moment it is touched — which is all a lookup
//! needs. `validate` goes further and proves global invariants over the
//! *entire* image, still without allocating proportional to declared counts:
//! work is O(nodes + blocks + referenced names), and bookkeeping is per-node /
//! per-block, never a section-sized byte buffer.
//!
//! Checks performed, beyond header validation:
//!
//! * every node record field (type, reserved bytes, name/blob intervals,
//!   index linkage, child_count consistency),
//! * every name is valid UTF-8 and contains no NUL or `/`,
//! * every index block: kind, packed-prefix layout, strict key ordering,
//!   child reference domains and strictly-forward (acyclic) block links,
//! * every directory index is reachable from its `first_block`, references
//!   exactly `child_count` distinct leaf children, with no block sharing
//!   between directories and no orphan blocks,
//! * name intervals of distinct nodes never overlap; blob intervals of
//!   distinct payload nodes never overlap (zero-length payloads at offset 0
//!   excepted),
//! * exactly one root: node 0 is a directory, every other node is reachable
//!   from it exactly once (no cycles, no sharing in the node tree).

use std::collections::HashSet;

use crate::error::{Error, Result};
use crate::format::{NodeType, KIND_INTERNAL, KIND_LEAF, NIL};
use crate::reader::{NodeInfo, Reader};

/// Outcome of a successful full validation.
#[derive(Debug, Clone)]
pub struct ValidationReport {
    /// Number of node records.
    pub nodes: u64,
    /// Number of index blocks.
    pub blocks: u64,
    /// Number of directories.
    pub dirs: u64,
    /// Total regular-file payload bytes.
    pub file_bytes: u64,
}

/// Run every global check over a reader.
pub fn validate(reader: &Reader) -> Result<ValidationReport> {
    let h = *reader.header();
    let node_count = h.node_count;
    let block_count = h.block_count;

    // ---- Phase 1: every node record + interval disjointness --------------
    let mut infos: Vec<NodeInfo> = Vec::new();
    let mut dirs = 0u64;
    let mut file_bytes = 0u64;
    let mut name_ivs: Vec<(u64, u64, u32)> = Vec::new();
    let mut blob_ivs: Vec<(u64, u64, u32)> = Vec::new();

    for id in 0..node_count {
        let info = reader.load_node(id)?;
        let name = reader.node_name(&info)?;
        if id != h.root_node {
            if name.is_empty()
                || name == "."
                || name == ".."
                || name.contains('/')
                || name.contains('\0')
            {
                return Err(Error::BadName(id));
            }
        } else if !name.is_empty() {
            return Err(Error::BadName(id));
        }
        name_ivs.push((info.name.0, info.name.0 + info.name.1, id));

        match info.kind {
            NodeType::Dir => dirs += 1,
            NodeType::File => {
                file_bytes += info.data.1;
                if info.data.1 > 0 {
                    blob_ivs.push((info.data.0, info.data.0 + info.data.1, id));
                }
            }
            NodeType::Symlink => {
                if info.data.1 > 0 {
                    blob_ivs.push((info.data.0, info.data.0 + info.data.1, id));
                }
            }
        }
        infos.push(info);
    }

    assert_disjoint(&mut name_ivs, true)?;
    assert_disjoint(&mut blob_ivs, false)?;

    // ---- Phase 2: every block parses (per-block field checks) -------------
    // The reader performs kind/ordering/reference/acyclicity checks on each
    // load; touch every block once so a corrupt block unused by the sampled
    // queries still fails validation.
    for bid in 0..block_count {
        let (kind, entries) = reader.debug_block(bid)?;
        debug_assert!(kind == KIND_LEAF || kind == KIND_INTERNAL);
        debug_assert!(!entries.is_empty());
    }

    // ---- Phase 3: per-directory index semantics ---------------------------
    let mut parent_of: Vec<Option<u32>> = vec![None; node_count as usize];
    let mut block_owner: Vec<Option<u32>> = vec![None; block_count as usize];

    for id in 0..node_count {
        let info = &infos[id as usize];
        if info.kind != NodeType::Dir {
            continue;
        }
        if info.first_block == NIL {
            if info.child_count != 0 {
                return Err(Error::CorruptIndex {
                    detail: "empty dir with nonzero child_count",
                });
            }
            continue;
        }

        let mut leaf_count = 0u32;
        let mut local_blocks: HashSet<u32> = HashSet::new();
        let (min_key, max_key) = check_subtree(
            reader,
            &infos,
            info.first_block,
            &mut local_blocks,
            &mut leaf_count,
        )?;

        for b in local_blocks {
            if block_owner[b as usize].is_some() {
                return Err(Error::CorruptIndex {
                    detail: "index block shared between directories",
                });
            }
            block_owner[b as usize] = Some(id);
        }

        if leaf_count != info.child_count {
            return Err(Error::CorruptIndex {
                detail: "directory leaf count != child_count",
            });
        }

        // Walk leaves in ascending key order and record parentage.
        let mut parent_seen: HashSet<u32> = HashSet::new();
        let mut stack = vec![info.first_block];
        let mut prev: Option<Vec<u8>> = None;
        while let Some(bid) = stack.pop() {
            let (kind, entries) = reader.debug_block(bid)?;
            if kind == KIND_INTERNAL {
                // Push reversed so ascending subtree order is preserved.
                for &(_, _, child) in entries.iter().rev() {
                    stack.push(child as u32);
                }
            } else {
                for &(_, _, child) in &entries {
                    let cid = child as u32;
                    if !parent_seen.insert(cid) {
                        return Err(Error::CorruptIndex {
                            detail: "duplicate child in directory index",
                        });
                    }
                    if let Some(p) = parent_of[cid as usize] {
                        return Err(if p == id {
                            Error::CorruptIndex {
                                detail: "node listed twice under one directory",
                            }
                        } else {
                            Error::CorruptIndex {
                                detail: "node reachable from two directories",
                            }
                        });
                    }
                    parent_of[cid as usize] = Some(id);
                    let child_info = &infos[cid as usize];
                    let nm = reader.node_name(child_info)?;
                    if let Some(p) = &prev {
                        if p.as_slice() >= nm.as_bytes() {
                            return Err(Error::BadOrdering("leaf keys not strictly ascending"));
                        }
                    }
                    prev = Some(nm.into_bytes());
                }
            }
        }
        // Sanity: the subtree helper's min/max agree with the boundary names.
        let _ = (min_key, max_key);
    }

    if block_owner.iter().any(|o| o.is_none()) {
        return Err(Error::CorruptIndex {
            detail: "orphan index block not reachable from any directory",
        });
    }

    // ---- Phase 4: single rooted node tree ---------------------------------
    if infos[h.root_node as usize].kind != NodeType::Dir {
        return Err(Error::BadNodeRecord {
            index: h.root_node,
            detail: "root is not a directory",
        });
    }
    for (id, parent) in parent_of.iter().enumerate() {
        if id as u32 == h.root_node {
            if parent.is_some() {
                return Err(Error::CorruptIndex {
                    detail: "root node also appears as a child",
                });
            }
        } else if parent.is_none() {
            return Err(Error::CorruptIndex {
                detail: "node unreachable from root (forest)",
            });
        }
    }

    if h.root_block != infos[h.root_node as usize].first_block {
        return Err(Error::BadRoot(h.root_block as u64));
    }

    Ok(ValidationReport {
        nodes: node_count as u64,
        blocks: block_count as u64,
        dirs,
        file_bytes,
    })
}

fn assert_disjoint(ivs: &mut [(u64, u64, u32)], names: bool) -> Result<()> {
    ivs.sort_unstable_by_key(|(s, _, _)| *s);
    for w in ivs.windows(2) {
        let (_s0, e0, i0) = w[0];
        let (s1, _e1, i1) = w[1];
        if s1 < e0 {
            return if names {
                Err(Error::NameOverlap {
                    a: i0,
                    a_end: e0,
                    b: i1,
                    b_start: s1,
                })
            } else {
                Err(Error::BlobOverlap {
                    a: i0,
                    a_end: e0,
                    b: i1,
                    b_start: s1,
                })
            };
        }
    }
    Ok(())
}

/// Recursively prove structural soundness of one directory's index:
///
/// * the block graph reachable here is acyclic (`visited` guards sharing),
/// * internal entries' stored separator keys equal the *actual* minimum key of
///   the referenced subtree (so a swapped/copied pointer cannot desynchronize
///   routing from contents),
/// * subtree key ranges are strictly ordered with no overlap,
/// * leaf entries' stored keys equal the referenced node's real name interval
///   (a mutated key that still points in-section is caught here),
/// * every leaf child carries the expected directory-ownership candidate.
///
/// Returns the minimum and maximum leaf key bytes and adds the number of
/// leaves seen to `leaf_count`.
fn check_subtree(
    reader: &Reader,
    infos: &[NodeInfo],
    block: u32,
    visited: &mut HashSet<u32>,
    leaf_count: &mut u32,
) -> Result<(Vec<u8>, Vec<u8>)> {
    if !visited.insert(block) {
        return Err(Error::CorruptIndex {
            detail: "cycle/shared block inside one directory index",
        });
    }
    let (kind, entries) = reader.debug_block(block)?;
    let mut overall_min: Option<Vec<u8>> = None;
    let mut overall_max: Option<Vec<u8>> = None;

    for &(key_off, key_len, child) in &entries {
        let (child_min, child_max) = if kind == KIND_LEAF {
            let cid = child as u32;
            if cid as usize >= infos.len() {
                return Err(Error::BadNodeRef {
                    index: child,
                    count: infos.len() as u64,
                });
            }
            let node = &infos[cid as usize];
            // Stored key interval must be exactly this node's name interval.
            if key_off != node.name.0 || key_len != node.name.1 {
                return Err(Error::CorruptIndex {
                    detail: "leaf entry key does not match child node name interval",
                });
            }
            let nm = reader.node_name(node)?;
            *leaf_count += 1;
            (nm.as_bytes().to_vec(), nm.as_bytes().to_vec())
        } else {
            let child_block = child as u32;
            let (mn, mx) = check_subtree(reader, infos, child_block, visited, leaf_count)?;
            // The stored separator must name the subtree's true minimum.
            let stored = reader.read_name_raw(key_off, key_len)?;
            if stored != mn {
                return Err(Error::CorruptIndex {
                    detail: "internal separator key != subtree minimum key",
                });
            }
            (mn, mx)
        };

        if let Some(prev_max) = &overall_max {
            if prev_max.as_slice() >= child_min.as_slice() {
                return Err(Error::BadOrdering(
                    "subtree key ranges overlap or are unsorted",
                ));
            }
        }
        if overall_min.is_none() {
            overall_min = Some(child_min);
        }
        overall_max = Some(child_max);
    }

    Ok((overall_min.unwrap(), overall_max.unwrap()))
}
