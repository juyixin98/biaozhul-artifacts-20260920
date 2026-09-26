//! Validating IFIX1 reader.
//!
//! Open sequence (see `docs/FORMAT.md` §6):
//! header fields → region tiling (checked arithmetic) → full TOC
//! validation → per-record checks → parent/overlap graph checks →
//! reachability/cycle guard → data-interval disjointness → per-blob
//! checksums → whole-file CRC. Nothing declared by the file drives an
//! allocation before it has been cross-checked against the measured file
//! length and [`Limits`]. The individual checks live in [`crate::validate`].

use crate::crc::Crc32;
use crate::error::{IfixError, Result};
use crate::format::{Limits, HEADER_LEN, ROOT_ID};
use crate::storage::Storage;
use crate::toc::{Toc, TocContext};
use crate::validate::{
    self, check_data_intervals, check_graph, check_reachability_and_names, parse_header,
    read_and_check_records, read_name_raw, verify_blob_hashes, verify_file_crc, NodeInfo,
};
use std::io::Write;

/// Metadata returned for a resolved path.
#[derive(Debug, Clone)]
pub struct NodeMeta {
    pub id: u32,
    pub name: String,
    pub is_dir: bool,
    pub size: u64,
    pub child_count: u16,
}

/// A validated, ready-to-serve index file.
pub struct IndexFile<S: Storage> {
    storage: S,
    limits: Limits,
    node_count: u32,
    data_bytes: u64,
    nodes_offset: u64,
    toc_offset: u64,
    file_len: u64,
    toc: Toc,
    nodes: Vec<NodeInfo>,
}

impl<S: Storage> std::fmt::Debug for IndexFile<S> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("IndexFile")
            .field("node_count", &self.node_count)
            .field("file_len", &self.file_len)
            .field("data_bytes", &self.data_bytes)
            .field("nodes_offset", &self.nodes_offset)
            .field("toc_offset", &self.toc_offset)
            .field("toc_height", &self.toc.height())
            .finish()
    }
}

impl<S: Storage> IndexFile<S> {
    /// Open and fully validate an index file.
    pub fn open(storage: S, limits: Limits) -> Result<IndexFile<S>> {
        let file_len = storage.len();
        if file_len < HEADER_LEN {
            return Err(IfixError::format(
                "TRUNCATED",
                format!("file is {file_len} bytes, smaller than the {HEADER_LEN}-byte header"),
            ));
        }
        if file_len > limits.max_file_bytes {
            return Err(IfixError::format(
                "LIMIT_FILE",
                format!("file is {file_len} bytes, limit {}", limits.max_file_bytes),
            ));
        }

        let mut raw = [0u8; HEADER_LEN as usize];
        storage.read_at(&mut raw, 0)?;
        let header = parse_header(&raw)?;

        // Region tiling, all in checked arithmetic against file_len.
        let data_end = HEADER_LEN
            .checked_add(header.data_bytes)
            .ok_or_else(|| IfixError::format("REGION_TILING", "data end overflow"))?;
        if data_end != header.nodes_offset {
            return Err(IfixError::format(
                "REGION_TILING",
                format!(
                    "data region end {data_end} != nodes_offset {}",
                    header.nodes_offset
                ),
            ));
        }
        if header.data_bytes > limits.max_data_bytes {
            return Err(IfixError::format(
                "LIMIT_DATA",
                format!("declared data {} exceeds limit", header.data_bytes),
            ));
        }
        if header.node_count as u64 > limits.max_nodes as u64 {
            return Err(IfixError::format(
                "LIMIT_NODES",
                format!("declared node_count {} exceeds limit", header.node_count),
            ));
        }
        let node_block = (header.node_count as u64)
            .checked_mul(48)
            .ok_or_else(|| IfixError::format("REGION_TILING", "node block size overflow"))?;
        let node_block_end = header
            .nodes_offset
            .checked_add(node_block)
            .ok_or_else(|| IfixError::format("REGION_TILING", "node block end overflow"))?;
        if node_block_end != header.toc_offset {
            return Err(IfixError::format(
                "REGION_TILING",
                format!(
                    "node block end {node_block_end} != toc_offset {}",
                    header.toc_offset
                ),
            ));
        }
        if header.toc_offset > file_len {
            return Err(IfixError::format(
                "REGION_TILING",
                "toc_offset past end of file",
            ));
        }
        let toc_len = file_len - header.toc_offset;
        if toc_len > limits.max_toc_bytes {
            return Err(IfixError::format(
                "LIMIT_TOC",
                format!("TOC region {toc_len} exceeds limit"),
            ));
        }

        let ctx = TocContext {
            toc_offset: header.toc_offset,
            file_len,
            nodes_offset: header.nodes_offset,
            node_count: header.node_count,
            header_log2_page: header.log2_page,
        };
        let toc = Toc::parse(&storage, ctx)?;

        // node_count is trusted only after tiling + TOC validation; the
        // per-node allocation is capped by limits.max_nodes.
        let nodes = read_and_check_records(&storage, &header, &limits)?;
        check_graph(&nodes)?;
        check_reachability_and_names(&storage, &nodes, header.node_count, &limits)?;
        let intervals = check_data_intervals(&nodes, header.data_bytes)?;
        verify_blob_hashes(&storage, &nodes, &intervals)?;
        verify_file_crc(&storage, file_len, header.file_crc)?;

        Ok(IndexFile {
            storage,
            limits,
            node_count: header.node_count,
            data_bytes: header.data_bytes,
            nodes_offset: header.nodes_offset,
            toc_offset: header.toc_offset,
            file_len,
            toc,
            nodes,
        })
    }

    pub fn node_count(&self) -> u32 {
        self.node_count
    }
    pub fn file_len(&self) -> u64 {
        self.file_len
    }
    pub fn toc_height(&self) -> usize {
        self.toc.height()
    }

    /// Resolve a node id to its 48-byte record file offset *through the
    /// multi-layer TOC* (one index page per level).
    pub fn resolve_record_offset(&self, id: u32) -> Result<u64> {
        self.toc.resolve(&self.storage, id, self.node_count)
    }

    /// Resolve a `/`-separated path to its node, using the TOC for every
    /// node record touched (only the target's ancestor chain is read).
    pub fn lookup(&self, path: &str) -> Result<NodeMeta> {
        let id = self.lookup_id(path)?;
        self.meta_of(id)
    }

    /// Resolve a path to its node id.
    pub fn lookup_id(&self, path: &str) -> Result<u32> {
        let mut current = ROOT_ID;
        let mut depth = 0u32;
        for component in path.split('/').filter(|p| !p.is_empty()) {
            depth += 1;
            if depth > self.limits.max_depth {
                return Err(IfixError::format(
                    "LIMIT_DEPTH",
                    format!("path deeper than {}", self.limits.max_depth),
                ));
            }
            if !self.nodes[current as usize].is_dir {
                return Err(IfixError::format(
                    "NOT_A_DIR",
                    format!("{component:?}: parent component is a file"),
                ));
            }
            current = self.find_child(current, component)?;
        }
        Ok(current)
    }

    /// Binary search a node's sorted children by name. Candidate records are
    /// resolved through the TOC; names are read from the data region.
    fn find_child(&self, parent_id: u32, name: &str) -> Result<u32> {
        let parent = &self.nodes[parent_id as usize];
        let count = parent.child_count as i64;
        if count == 0 {
            return Err(IfixError::NotFound {
                path: name.to_owned(),
            });
        }
        let base = parent.first_child as i64;
        let (mut lo, mut hi) = (0i64, count - 1);
        while lo <= hi {
            let mid = lo + (hi - lo) / 2;
            let candidate_id = (base + mid) as u32;
            let record_offset = self
                .toc
                .resolve(&self.storage, candidate_id, self.node_count)?;
            debug_assert_eq!(
                record_offset,
                self.nodes_offset + (candidate_id as u64) * 48
            );
            let candidate_name = self.read_name(&self.nodes[candidate_id as usize])?;
            match candidate_name.as_str().cmp(name) {
                std::cmp::Ordering::Equal => return Ok(candidate_id),
                std::cmp::Ordering::Greater => hi = mid - 1,
                std::cmp::Ordering::Less => lo = mid + 1,
            }
        }
        Err(IfixError::NotFound {
            path: name.to_owned(),
        })
    }

    fn read_name(&self, node: &NodeInfo) -> Result<String> {
        read_name_raw(&self.storage, node)
    }

    fn meta_of(&self, id: u32) -> Result<NodeMeta> {
        let info = &self.nodes[id as usize];
        Ok(NodeMeta {
            id,
            name: self.read_name(info)?,
            is_dir: info.is_dir,
            size: info.data_len,
            child_count: info.child_count,
        })
    }

    /// List the direct children of a directory.
    pub fn list(&self, path: &str) -> Result<Vec<NodeMeta>> {
        let id = self.lookup_id(path)?;
        let info = &self.nodes[id as usize];
        if !info.is_dir {
            return Err(IfixError::format(
                "NOT_A_DIR",
                format!("{path:?} is a file"),
            ));
        }
        let mut out = Vec::with_capacity(info.child_count as usize);
        for child_id in info.first_child..info.first_child + info.child_count as u32 {
            out.push(self.meta_of(child_id)?);
        }
        Ok(out)
    }

    /// Stream a file node's blob into `out`, re-verifying its CRC-32 and
    /// refusing to produce more than `max_output_bytes`.
    pub fn read_blob<W: Write>(&self, path: &str, out: &mut W) -> Result<u64> {
        let id = self.lookup_id(path)?;
        self.read_blob_id(id, out)
    }

    pub fn read_blob_id<W: Write>(&self, id: u32, out: &mut W) -> Result<u64> {
        let info = &self.nodes[id as usize];
        if info.is_dir {
            return Err(IfixError::format(
                "NOT_A_FILE",
                "cannot read a directory as a blob",
            ));
        }
        if info.data_len > self.limits.max_output_bytes {
            return Err(IfixError::format(
                "LIMIT_OUTPUT",
                format!(
                    "blob is {} bytes, output limit {}",
                    info.data_len, self.limits.max_output_bytes
                ),
            ));
        }
        let mut remaining = info.data_len;
        let mut offset = info.data_offset;
        let mut hasher = Crc32::new();
        let mut buf = vec![0u8; validate::IO_BUF.min(remaining.max(1) as usize)];
        let mut produced: u64 = 0;
        while remaining > 0 {
            let take = (remaining as usize).min(buf.len());
            self.storage.read_at(&mut buf[..take], offset)?;
            hasher.update(&buf[..take]);
            out.write_all(&buf[..take])?;
            produced += take as u64;
            if produced > self.limits.max_output_bytes {
                return Err(IfixError::format(
                    "LIMIT_OUTPUT",
                    format!("output exceeded {} bytes", self.limits.max_output_bytes),
                ));
            }
            remaining -= take as u64;
            offset += take as u64;
        }
        if hasher.finish() != info.blob_hash {
            return Err(IfixError::format(
                "BLOB_CRC",
                format!("node {id} blob CRC-32 mismatch"),
            ));
        }
        Ok(produced)
    }

    /// Iterate every node id in validated BFS order.
    pub fn iter_ids(&self) -> impl Iterator<Item = u32> {
        0..self.node_count
    }

    pub fn data_bytes(&self) -> u64 {
        self.data_bytes
    }
    pub fn toc_offset(&self) -> u64 {
        self.toc_offset
    }
    pub fn nodes_offset(&self) -> u64 {
        self.nodes_offset
    }
}
