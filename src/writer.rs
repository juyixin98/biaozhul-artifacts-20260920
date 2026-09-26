//! Streaming IFIX1 encoder.
//!
//! Nodes are laid out breadth-first, which makes every node's direct-child
//! id interval contiguous (`first_child .. first_child + child_count`).
//! Blob bytes are streamed through a fixed 64 KiB buffer; only the bounded
//! per-node metadata table lives in memory.

use crate::crc::Crc32;
use crate::error::{IfixError, Result};
use crate::format::{range_within, Limits, HEADER_LEN, MAGIC, MAX_CHILDREN_HARD, VERSION};
use crate::model::{Blob, BuildRequest, InputNode, PreparedTree};
use std::collections::VecDeque;
use std::io::{Read, Seek, SeekFrom, Write};

const COPY_BUF: usize = 64 * 1024;
const NONE: u32 = 0xFFFF_FFFF;

const NODE_FILE: u8 = 1;
const NODE_DIR: u8 = 2;

/// Per-node metadata retained while writing (bounded by `Limits::max_nodes`).
#[derive(Debug, Clone)]
struct Layout {
    data_offset: u64,
    data_len: u64,
    blob_hash: u32,
    first_child: u32,
    child_count: u16,
    node_type: u8,
    name_offset: u64,
    name_len: u8,
}

/// Statistics returned after a successful write.
#[derive(Debug, Clone)]
pub struct WriterStats {
    pub file_len: u64,
    pub node_count: u32,
    pub data_bytes: u64,
    pub toc_levels: u32,
}

/// Encode a prepared tree into `out`, streaming blob content through
/// `resolver` for `content_file` nodes.
pub fn write_index<W: Write + Seek>(
    req: &BuildRequest,
    prepared: &PreparedTree,
    resolver: &dyn crate::model::BlobResolver,
    out: &mut W,
    limits: &Limits,
) -> Result<WriterStats> {
    if !(5..=16).contains(&req.log2_page) {
        return Err(IfixError::format(
            "BAD_FANOUT",
            format!("log2_page must be in 5..=16, got {}", req.log2_page),
        ));
    }

    // Reserve the header; data region starts at HEADER_LEN.
    out.seek(SeekFrom::Start(0))?;
    out.write_all(&[0u8; HEADER_LEN as usize])?;

    let mut hasher = Crc32::new();
    let mut data_bytes: u64 = 0;
    let mut any_blob = false;

    // Breadth-first layout. Ids are assigned at ENQUEUE time: a node's direct
    // children are enqueued as one contiguous batch, and while a level is
    // processed only that level's child batches are produced, so each node's
    // child id interval is contiguous (the invariant the decoder checks).
    let mut layouts: Vec<Layout> = Vec::new();
    let mut queue: VecDeque<(u32, &InputNode)> = VecDeque::new();
    let mut next_id: u32 = 0;
    queue.push_back((next_id, &prepared.root));
    next_id += 1;

    while let Some((id, node)) = queue.pop_front() {
        debug_assert_eq!(id as usize, layouts.len(), "pop order must match id order");
        let child_count = node.children.len();
        if child_count > MAX_CHILDREN_HARD {
            return Err(IfixError::format(
                "LIMIT_CHILDREN",
                format!("node {id} has {child_count} children (max {MAX_CHILDREN_HARD})"),
            ));
        }
        // The direct-child batch receives [next_id, next_id + count) now.
        let first_child = if child_count == 0 { NONE } else { next_id };
        let payload = write_node_payload(
            node,
            out,
            resolver,
            limits,
            &mut data_bytes,
            &mut hasher,
            &mut any_blob,
        )?;
        layouts.push(Layout {
            first_child,
            child_count: child_count as u16,
            ..payload
        });
        for child in &node.children {
            queue.push_back((next_id, child));
            next_id = next_id
                .checked_add(1)
                .ok_or_else(|| IfixError::format("LIMIT_NODES", "node id overflow in layout"))?;
        }
    }

    let node_count = layouts.len() as u32;
    if node_count != prepared.node_count {
        return Err(IfixError::format(
            "INTERNAL",
            "BFS node count disagrees with validated count",
        ));
    }

    let nodes_offset = checked_offset(HEADER_LEN, data_bytes, limits.max_file_bytes, "nodes")?;

    // Node block.
    let mut record_buf = [0u8; 48];
    for (id, l) in layouts.iter().enumerate() {
        validate_layout_invariants(id as u32, l, data_bytes)?;
        encode_record(&mut record_buf, l);
        hasher.update(&record_buf);
        out.write_all(&record_buf)?;
    }

    let node_block_bytes = (node_count as u64)
        .checked_mul(48)
        .ok_or_else(|| IfixError::format("INTERNAL", "node block size overflow"))?;
    let toc_offset = checked_offset(nodes_offset, node_block_bytes, limits.max_file_bytes, "toc")?;

    // TOC, bottom-up.
    let fanout: usize = 1usize << req.log2_page;
    let mut level_lens: Vec<u64> = Vec::new();
    let mut level_offsets: Vec<u64> = Vec::new();

    let mut current: Vec<(u32, u64)> = (0..node_count)
        .map(|id| (id, nodes_offset + (id as u64) * 48))
        .collect();
    let mut cursor_offset = toc_offset;
    loop {
        let level_bytes = (current.len() as u64)
            .checked_mul(12)
            .ok_or_else(|| IfixError::format("INTERNAL", "TOC level size overflow"))?;
        if cursor_offset
            .checked_add(level_bytes)
            .map(|end| {
                end > limits.max_file_bytes || end > toc_offset.saturating_add(limits.max_toc_bytes)
            })
            .unwrap_or(true)
        {
            return Err(IfixError::format(
                "LIMIT_TOC",
                "TOC exceeds configured size limit",
            ));
        }
        let child_level_offset = cursor_offset;
        level_offsets.push(child_level_offset);
        level_lens.push(level_bytes);
        let mut entry_buf = [0u8; 12];
        for &(key, payload) in &current {
            entry_buf[0..4].copy_from_slice(&key.to_le_bytes());
            entry_buf[4..12].copy_from_slice(&payload.to_le_bytes());
            hasher.update(&entry_buf);
            out.write_all(&entry_buf)?;
        }
        cursor_offset += level_bytes;

        if current.len() == 1 {
            break;
        }
        let parents: Vec<(u32, u64)> = current
            .chunks(fanout)
            .enumerate()
            .map(|(page, chunk)| {
                let first_key = chunk[0].0;
                let child_entry = child_level_offset + (page * fanout) as u64 * 12;
                (first_key, child_entry)
            })
            .collect();
        current = parents;
    }

    // Tail: level lengths, level count, fanout, TEND.
    let tail_start = cursor_offset;
    let mut tail = Vec::with_capacity(level_lens.len() * 8 + 9);
    for len in &level_lens {
        tail.extend_from_slice(&len.to_le_bytes());
    }
    tail.extend_from_slice(&(level_lens.len() as u32).to_le_bytes());
    tail.push(req.log2_page);
    tail.extend_from_slice(b"TEND");
    hasher.update(&tail);
    out.write_all(&tail)?;
    let file_len = tail_start
        .checked_add(tail.len() as u64)
        .ok_or_else(|| IfixError::format("INTERNAL", "file length overflow"))?;

    if file_len > limits.max_file_bytes {
        return Err(IfixError::format(
            "LIMIT_FILE",
            format!(
                "file size {file_len} exceeds limit {}",
                limits.max_file_bytes
            ),
        ));
    }
    let file_crc = hasher.finish();

    // Header, written last.
    let mut header = [0u8; HEADER_LEN as usize];
    header[0..6].copy_from_slice(&MAGIC);
    header[6] = if any_blob { 1 } else { 0 };
    header[7] = req.log2_page;
    header[8..10].copy_from_slice(&VERSION.to_le_bytes());
    header[12..16].copy_from_slice(&node_count.to_le_bytes());
    header[16..24].copy_from_slice(&data_bytes.to_le_bytes());
    header[24..32].copy_from_slice(&nodes_offset.to_le_bytes());
    header[32..40].copy_from_slice(&toc_offset.to_le_bytes());
    header[40..44].copy_from_slice(&file_crc.to_le_bytes());
    // header[44..48] stays zero (reserved).
    out.seek(SeekFrom::Start(0))?;
    out.write_all(&header)?;
    out.flush()?;

    Ok(WriterStats {
        file_len,
        node_count,
        data_bytes,
        toc_levels: level_lens.len() as u32,
    })
}

fn checked_offset(base: u64, add: u64, max: u64, what: &str) -> Result<u64> {
    base.checked_add(add).filter(|&v| v <= max).ok_or_else(|| {
        IfixError::format(
            "LIMIT_FILE",
            format!("{what} offset exceeds file size limit"),
        )
    })
}

fn validate_layout_invariants(id: u32, l: &Layout, data_bytes: u64) -> Result<()> {
    let data_end = HEADER_LEN
        .checked_add(data_bytes)
        .ok_or_else(|| IfixError::format("INTERNAL", "data end overflow"))?;
    match l.node_type {
        NODE_DIR => {
            if l.data_offset != 0 || l.data_len != 0 || l.blob_hash != 0 {
                return Err(IfixError::format(
                    "INTERNAL",
                    format!("dir node {id} carries blob data"),
                ));
            }
        }
        NODE_FILE => {
            if l.data_len > 0 && !range_within(l.data_offset, l.data_len, data_end) {
                return Err(IfixError::format(
                    "INTERNAL",
                    format!("file node {id} blob escapes data region"),
                ));
            }
            if l.data_len == 0 && (l.data_offset != 0 || l.blob_hash != 0) {
                return Err(IfixError::format(
                    "INTERNAL",
                    format!("empty file node {id} has nonzero offset/hash"),
                ));
            }
        }
        other => {
            return Err(IfixError::format(
                "INTERNAL",
                format!("node {id} has invalid type {other}"),
            ))
        }
    }
    if !range_within(l.name_offset, l.name_len as u64, data_end) {
        return Err(IfixError::format(
            "INTERNAL",
            format!("node {id} name escapes data region"),
        ));
    }
    Ok(())
}

fn encode_record(buf: &mut [u8; 48], l: &Layout) {
    buf[0..8].copy_from_slice(&l.data_offset.to_le_bytes());
    buf[8..16].copy_from_slice(&l.data_len.to_le_bytes());
    buf[16..20].copy_from_slice(&l.blob_hash.to_le_bytes());
    buf[20..24].copy_from_slice(&0u32.to_le_bytes()); // reserved
    buf[24..28].copy_from_slice(&l.first_child.to_le_bytes());
    buf[28..30].copy_from_slice(&l.child_count.to_le_bytes());
    buf[30] = l.node_type;
    buf[31] = l.name_len;
    buf[32..34].copy_from_slice(&0u16.to_le_bytes()); // flags
    buf[34..36].copy_from_slice(&0u16.to_le_bytes()); // reserved
    let check = Crc32::checksum(&buf[0..36]);
    buf[36..40].copy_from_slice(&check.to_le_bytes());
    buf[40..48].copy_from_slice(&l.name_offset.to_le_bytes());
}

/// Write one node's data-region payload (name bytes, then file blob) and
/// return its layout.
#[allow(clippy::too_many_arguments)]
fn write_node_payload<W: Write>(
    node: &InputNode,
    out: &mut W,
    resolver: &dyn crate::model::BlobResolver,
    limits: &Limits,
    data_bytes: &mut u64,
    hasher: &mut Crc32,
    any_blob: &mut bool,
) -> Result<Layout> {
    let name_offset = HEADER_LEN + *data_bytes;
    let name_len = node.name.len();
    if name_len > u8::MAX as usize {
        return Err(IfixError::format("NAME_LEN", "name longer than 255 bytes"));
    }
    if !node.name.is_empty() {
        hasher.update(node.name.as_bytes());
        out.write_all(node.name.as_bytes())?;
        add_data_bytes(data_bytes, name_len as u64, limits)?;
    }

    let (data_offset, data_len, blob_hash) = if node.is_dir {
        (0u64, 0u64, 0u32)
    } else {
        match &node.blob {
            Blob::Empty => (0, 0, 0),
            Blob::Inline(bytes) if bytes.is_empty() => (0, 0, 0),
            Blob::Inline(bytes) => {
                let off = HEADER_LEN + *data_bytes;
                hasher.update(bytes);
                out.write_all(bytes)?;
                add_data_bytes(data_bytes, bytes.len() as u64, limits)?;
                *any_blob = true;
                (off, bytes.len() as u64, Crc32::checksum(bytes))
            }
            Blob::File(path) => {
                let off = HEADER_LEN + *data_bytes;
                let mut stream = resolver.open(path)?;
                let mut blob_hasher = Crc32::new();
                let mut buf = vec![0u8; COPY_BUF];
                let mut total: u64 = 0;
                loop {
                    let n = stream.read(&mut buf)?;
                    if n == 0 {
                        break;
                    }
                    blob_hasher.update(&buf[..n]);
                    hasher.update(&buf[..n]);
                    out.write_all(&buf[..n])?;
                    total = total
                        .checked_add(n as u64)
                        .ok_or_else(|| IfixError::format("LIMIT_DATA", "blob size overflow"))?;
                    add_data_bytes(data_bytes, n as u64, limits)?;
                }
                *any_blob |= total > 0;
                (off, total, blob_hasher.finish())
            }
        }
    };

    Ok(Layout {
        data_offset,
        data_len,
        blob_hash,
        first_child: NONE,
        child_count: 0,
        node_type: if node.is_dir { NODE_DIR } else { NODE_FILE },
        name_offset,
        name_len: name_len as u8,
    })
}

fn add_data_bytes(data_bytes: &mut u64, add: u64, limits: &Limits) -> Result<()> {
    *data_bytes = data_bytes
        .checked_add(add)
        .ok_or_else(|| IfixError::format("LIMIT_DATA", "data size overflow"))?;
    if *data_bytes > limits.max_data_bytes {
        return Err(IfixError::format(
            "LIMIT_DATA",
            format!("data region exceeds {} bytes", limits.max_data_bytes),
        ));
    }
    Ok(())
}
