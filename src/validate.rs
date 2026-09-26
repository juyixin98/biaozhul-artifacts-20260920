//! Structural and cryptographic validation of an opened IFIX1 file.
//!
//! This is the trust boundary: every offset, count and interval declared by
//! an untrusted file is checked here before the reader serves anything, and
//! no allocation is sized from a declared value before it has been
//! cross-checked against the measured file length and [`Limits`].

use crate::crc::Crc32;
use crate::error::{IfixError, Result};
use crate::format::{
    range_within, u16le, u32le, u64le, Limits, HEADER_LEN, MAGIC, MAX_CHILDREN_HARD, NONE_ID,
    ROOT_ID, VERSION,
};
use crate::storage::Storage;

pub(crate) const IO_BUF: usize = 64 * 1024;
pub(crate) const NODE_FILE: u8 = 1;
pub(crate) const NODE_DIR: u8 = 2;

/// Compact per-node view retained after a full validation pass.
#[derive(Debug, Clone)]
pub(crate) struct NodeInfo {
    pub id: u32,
    pub data_offset: u64,
    pub data_len: u64,
    pub blob_hash: u32,
    pub first_child: u32,
    pub child_count: u16,
    pub is_dir: bool,
    pub name_offset: u64,
    pub name_len: u8,
}

#[derive(Debug)]
pub(crate) struct Header {
    pub log2_page: u8,
    pub node_count: u32,
    pub data_bytes: u64,
    pub nodes_offset: u64,
    pub toc_offset: u64,
    pub file_crc: u32,
}

pub(crate) fn parse_header(raw: &[u8; 48]) -> Result<Header> {
    if raw[0..6] != MAGIC {
        return Err(IfixError::format(
            "BAD_MAGIC",
            format!("not an IFIX file (magic {:02X?})", &raw[0..6]),
        ));
    }
    let flags = raw[6];
    if flags & !1 != 0 {
        return Err(IfixError::format(
            "BAD_FLAGS",
            format!("reserved flag bits set: {flags:#04x}"),
        ));
    }
    let log2_page = raw[7];
    if !(5..=16).contains(&log2_page) {
        return Err(IfixError::format(
            "BAD_FANOUT",
            format!("log2_page {log2_page} outside 5..=16"),
        ));
    }
    let version = u16le(&raw[8..10]);
    if version != VERSION {
        return Err(IfixError::format(
            "BAD_VERSION",
            format!("unsupported version {version}, expected {VERSION}"),
        ));
    }
    if u16le(&raw[10..12]) != 0 {
        return Err(IfixError::format("BAD_HEADER", "reserved0 must be zero"));
    }
    if u32le(&raw[44..48]) != 0 {
        return Err(IfixError::format(
            "BAD_HEADER",
            "reserved bytes 44..48 must be zero",
        ));
    }
    Ok(Header {
        log2_page,
        node_count: u32le(&raw[12..16]),
        data_bytes: u64le(&raw[16..24]),
        nodes_offset: u64le(&raw[24..32]),
        toc_offset: u64le(&raw[32..40]),
        file_crc: u32le(&raw[40..44]),
    })
}

/// Read every fixed-size record, verify its self-check and all per-node
/// ranges/types. Records are streamed with a single 48-byte buffer.
pub(crate) fn read_and_check_records(
    storage: &dyn Storage,
    header: &Header,
    limits: &Limits,
) -> Result<Vec<NodeInfo>> {
    let data_end = HEADER_LEN + header.data_bytes;
    let mut nodes = Vec::with_capacity(header.node_count as usize);
    let mut buf = [0u8; 48];
    for id in 0..header.node_count {
        let offset = header.nodes_offset + (id as u64) * 48;
        storage.read_at(&mut buf, offset)?;
        let info = decode_record(&buf, id)?;

        if info.is_dir {
            if info.data_offset != 0 || info.data_len != 0 || info.blob_hash != 0 {
                return Err(IfixError::format(
                    "NODE_TYPE",
                    format!("dir node {id} must not carry blob data"),
                ));
            }
        } else if info.data_len == 0 {
            if info.data_offset != 0 || info.blob_hash != 0 {
                return Err(IfixError::format(
                    "NODE_TYPE",
                    format!("empty file node {id} must have zero offset and hash"),
                ));
            }
        } else if !range_within(info.data_offset, info.data_len, data_end)
            || info.data_offset < HEADER_LEN
        {
            return Err(IfixError::format(
                "DATA_RANGE",
                format!(
                    "node {id} blob [{}, +{}) outside data region [{}, {})",
                    info.data_offset, info.data_len, HEADER_LEN, data_end
                ),
            ));
        }

        if info.data_len > limits.max_output_bytes {
            return Err(IfixError::format(
                "LIMIT_OUTPUT",
                format!("node {id} blob {} exceeds output limit", info.data_len),
            ));
        }

        if info.name_len as usize > limits.max_name_bytes {
            return Err(IfixError::format(
                "LIMIT_NAME",
                format!("node {id} name exceeds name limit"),
            ));
        }
        if !range_within(info.name_offset, info.name_len as u64, data_end)
            || (info.name_len > 0 && info.name_offset < HEADER_LEN)
        {
            return Err(IfixError::format(
                "NAME_RANGE",
                format!("node {id} name range outside data region"),
            ));
        }
        if id == ROOT_ID && info.name_len != 0 {
            return Err(IfixError::format(
                "NAME_INVALID",
                "root node name must be empty",
            ));
        }
        if id == ROOT_ID && !info.is_dir {
            return Err(IfixError::format(
                "NODE_TYPE",
                "root node (id 0) must be a directory",
            ));
        }

        match (info.first_child, info.child_count) {
            (NONE_ID, 0) => {}
            (fc, 0) if fc != NONE_ID => {
                return Err(IfixError::format(
                    "CHILD_RANGE",
                    format!("node {id} has child_count 0 but first_child {fc}"),
                ))
            }
            (NONE_ID, cc) => {
                return Err(IfixError::format(
                    "CHILD_RANGE",
                    format!("node {id} has {cc} children but first_child is the none-sentinel"),
                ))
            }
            (fc, cc) => {
                let count = cc as u64;
                let end = (fc as u64)
                    .checked_add(count)
                    .ok_or_else(|| IfixError::format("CHILD_RANGE", "child interval overflow"))?;
                if end > header.node_count as u64 {
                    return Err(IfixError::format(
                        "CHILD_RANGE",
                        format!("node {id} child interval [{fc}, {end}) >= node_count"),
                    ));
                }
                if fc == id && cc > 0 {
                    return Err(IfixError::format(
                        "NODE_CYCLE",
                        format!("node {id} lists itself as a child"),
                    ));
                }
            }
        }

        nodes.push(info);
    }
    Ok(nodes)
}

fn decode_record(buf: &[u8; 48], id: u32) -> Result<NodeInfo> {
    let claimed = u32le(&buf[36..40]);
    let actual = Crc32::checksum(&buf[0..36]);
    if claimed != actual {
        return Err(IfixError::format(
            "NODE_CRC",
            format!("node {id} self_check {claimed:#010x} != computed {actual:#010x}"),
        ));
    }
    let node_type = buf[30];
    let is_dir = match node_type {
        NODE_FILE => false,
        NODE_DIR => true,
        other => {
            return Err(IfixError::format(
                "NODE_TYPE",
                format!("node {id} has invalid type byte {other}"),
            ))
        }
    };
    if u16le(&buf[32..34]) != 0 || u16le(&buf[34..36]) != 0 || u32le(&buf[20..24]) != 0 {
        return Err(IfixError::format(
            "NODE_FLAGS",
            format!("node {id} reserved bits are nonzero"),
        ));
    }
    let child_count = u16le(&buf[28..30]);
    if child_count as usize > MAX_CHILDREN_HARD {
        return Err(IfixError::format(
            "CHILD_RANGE",
            format!("node {id} child_count {child_count} exceeds hard maximum"),
        ));
    }
    Ok(NodeInfo {
        id,
        data_offset: u64le(&buf[0..8]),
        data_len: u64le(&buf[8..16]),
        blob_hash: u32le(&buf[16..20]),
        first_child: u32le(&buf[24..28]),
        child_count,
        is_dir,
        name_offset: u64le(&buf[40..48]),
        name_len: buf[31],
    })
}

/// Every non-root node must have exactly one parent; child intervals may
/// never overlap. Root must be parentless.
pub(crate) fn check_graph(nodes: &[NodeInfo]) -> Result<()> {
    const UNCLAIMED: u32 = u32::MAX;
    let n = nodes.len();
    let mut parent_of = vec![UNCLAIMED; n];
    for node in nodes {
        if node.child_count == 0 {
            continue;
        }
        for child in node.first_child..node.first_child + node.child_count as u32 {
            let slot = &mut parent_of[child as usize];
            if *slot != UNCLAIMED {
                return Err(IfixError::format(
                    "CHILD_OVERLAP",
                    format!(
                        "node {child} is claimed by both node {} and node {}",
                        *slot, node.id
                    ),
                ));
            }
            *slot = node.id;
        }
    }
    if parent_of[ROOT_ID as usize] != UNCLAIMED {
        return Err(IfixError::format(
            "GRAPH",
            format!(
                "root node must not have a parent (claimed by node {})",
                parent_of[0]
            ),
        ));
    }
    for (id, parent) in parent_of.iter().enumerate().skip(1) {
        if *parent == UNCLAIMED {
            return Err(IfixError::format(
                "ORPHAN_NODE",
                format!("node {id} has no parent"),
            ));
        }
    }
    Ok(())
}

/// Reachability from root with an explicit stack, depth guard, UTF-8 name
/// checks and strict sibling ordering/uniqueness.
pub(crate) fn check_reachability_and_names(
    storage: &dyn Storage,
    nodes: &[NodeInfo],
    node_count: u32,
    limits: &Limits,
) -> Result<()> {
    let mut reached = vec![false; node_count as usize];
    let mut stack: Vec<(u32, u32)> = vec![(ROOT_ID, 0)];
    let mut total = 0u32;
    while let Some((id, depth)) = stack.pop() {
        if depth > limits.max_depth {
            return Err(IfixError::format(
                "LIMIT_DEPTH",
                format!("tree depth exceeds {} (cycle guard)", limits.max_depth),
            ));
        }
        if reached[id as usize] {
            // Already proven impossible by parent counting; defensive.
            return Err(IfixError::format(
                "NODE_CYCLE",
                format!("node {id} reached twice"),
            ));
        }
        reached[id as usize] = true;
        total += 1;

        let node = &nodes[id as usize];
        if node.is_dir {
            let mut prev: Option<String> = None;
            for child_id in node.first_child..node.first_child + node.child_count as u32 {
                let child = &nodes[child_id as usize];
                let name = read_name_raw(storage, child)?;
                if let Some(p) = &prev {
                    match p.cmp(&name) {
                        std::cmp::Ordering::Less => {}
                        std::cmp::Ordering::Equal => {
                            return Err(IfixError::format(
                                "NAME_ORDER",
                                format!("duplicate sibling name {name:?} under node {id}"),
                            ))
                        }
                        std::cmp::Ordering::Greater => {
                            return Err(IfixError::format(
                                "NAME_ORDER",
                                format!(
                                    "sibling names under node {id} are not sorted: {p:?} > {name:?}"
                                ),
                            ))
                        }
                    }
                }
                prev = Some(name);
            }
        } else if node.child_count != 0 {
            return Err(IfixError::format(
                "NODE_TYPE",
                format!("file node {id} must not have children"),
            ));
        }

        let children = node.first_child..node.first_child + node.child_count as u32;
        for child_id in children.rev() {
            stack.push((child_id, depth + 1));
        }
    }
    if total != node_count {
        let first = reached.iter().position(|r| !*r).unwrap_or(0);
        return Err(IfixError::format(
            "ORPHAN_NODE",
            format!("node {first} is unreachable from root"),
        ));
    }
    Ok(())
}

pub(crate) fn read_name_raw(storage: &dyn Storage, node: &NodeInfo) -> Result<String> {
    if node.name_len == 0 {
        return Ok(String::new());
    }
    let mut buf = vec![0u8; node.name_len as usize];
    storage.read_at(&mut buf, node.name_offset)?;
    String::from_utf8(buf).map_err(|_| {
        IfixError::format(
            "NAME_INVALID",
            format!("node {} name is not valid UTF-8", node.id),
        )
    })
}

/// Verify every declared data interval (names and blobs) lies inside the
/// region and that intervals are pairwise disjoint. Returns the blob
/// intervals sorted by offset (names excluded).
pub(crate) fn check_data_intervals(
    nodes: &[NodeInfo],
    data_bytes: u64,
) -> Result<Vec<(u64, u64, u32)>> {
    let region_end = HEADER_LEN + data_bytes;
    let mut intervals: Vec<(u64, u64)> = Vec::with_capacity(nodes.len() * 2);
    let mut blobs: Vec<(u64, u64, u32)> = Vec::new();
    for node in nodes {
        if node.name_len > 0 {
            intervals.push((node.name_offset, node.name_len as u64));
        }
        if node.data_len > 0 {
            intervals.push((node.data_offset, node.data_len));
            blobs.push((node.data_offset, node.data_len, node.id));
        }
    }
    intervals.sort_unstable();
    let mut prev_end = HEADER_LEN;
    for (offset, len) in intervals {
        if offset < prev_end {
            return Err(IfixError::format(
                "DATA_OVERLAP",
                format!("data intervals overlap at offset {offset}"),
            ));
        }
        let end = offset
            .checked_add(len)
            .ok_or_else(|| IfixError::format("DATA_RANGE", "data interval overflow"))?;
        if end > region_end {
            return Err(IfixError::format(
                "DATA_RANGE",
                "data interval extends past the data region",
            ));
        }
        prev_end = end;
    }
    blobs.sort_unstable();
    Ok(blobs)
}

/// Verify every blob's CRC-32 with a fixed-size streaming buffer. Because
/// blob intervals are disjoint, total bytes read equal at most one data
/// region's worth.
pub(crate) fn verify_blob_hashes(
    storage: &dyn Storage,
    nodes: &[NodeInfo],
    blobs: &[(u64, u64, u32)],
) -> Result<()> {
    let mut buf = vec![0u8; IO_BUF];
    for &(offset, len, id) in blobs {
        let expected = nodes[id as usize].blob_hash;
        let mut hasher = Crc32::new();
        let mut remaining = len;
        let mut cursor = offset;
        while remaining > 0 {
            let take = (remaining as usize).min(buf.len());
            storage.read_at(&mut buf[..take], cursor)?;
            hasher.update(&buf[..take]);
            remaining -= take as u64;
            cursor += take as u64;
        }
        if hasher.finish() != expected {
            return Err(IfixError::format(
                "BLOB_CRC",
                format!("node {id} blob CRC-32 mismatch"),
            ));
        }
    }
    Ok(())
}

/// CRC-32 over bytes 48..file_len, streamed in fixed-size reads.
pub(crate) fn verify_file_crc(storage: &dyn Storage, file_len: u64, expected: u32) -> Result<()> {
    let mut hasher = Crc32::new();
    let mut buf = vec![0u8; IO_BUF];
    let mut cursor = HEADER_LEN;
    while cursor < file_len {
        let take = ((file_len - cursor) as usize).min(buf.len());
        storage.read_at(&mut buf[..take], cursor)?;
        hasher.update(&buf[..take]);
        cursor += take as u64;
    }
    let actual = hasher.finish();
    if actual != expected {
        return Err(IfixError::format(
            "BAD_CRC",
            format!("file CRC-32 {actual:#010x} != header {expected:#010x}"),
        ));
    }
    Ok(())
}
