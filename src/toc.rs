//! Table-of-contents (multi-layer offset index): parsing, full structural
//! validation and paged navigation by node id.
//!
//! Storage order (bottom-up): leaf level, internal levels, then the tail
//! `[level_lens: 8*L bytes][level_count: u32][fanout: u8][b"TEND"]`.

use crate::error::{IfixError, Result};
use crate::format::range_within;
use crate::storage::Storage;

const ENTRY: u64 = 12;
const TEND: [u8; 4] = *b"TEND";

/// Parsed, already-validated TOC geometry.
#[derive(Debug)]
pub struct Toc {
    pub fanout: u32,
    /// Absolute file offset of each level, leaves first.
    pub level_start: Vec<u64>,
    /// Entry count of each level.
    pub level_entries: Vec<u64>,
}

/// Raw header fields the TOC cross-checks against.
#[derive(Debug, Clone, Copy)]
pub struct TocContext {
    pub toc_offset: u64,
    pub file_len: u64,
    pub nodes_offset: u64,
    pub node_count: u32,
    pub header_log2_page: u8,
}

/// One index page read off storage.
struct Page {
    /// Global entry index of the page's first entry within its level.
    base_index: usize,
    entries: Vec<[u8; 12]>,
}

impl Page {
    fn key(&self, within: usize) -> u32 {
        u32::from_le_bytes(self.entries[within][0..4].try_into().unwrap())
    }
    fn payload(&self, within: usize) -> u64 {
        u64::from_le_bytes(self.entries[within][4..12].try_into().unwrap())
    }
}

impl Toc {
    /// Parse and fully validate the TOC. Only `O(number of levels)` small
    /// allocations are driven by file-declared metadata; entry payloads are
    /// streamed page by page with bounded buffers.
    pub fn parse<S: Storage>(s: &S, ctx: TocContext) -> Result<Toc> {
        let toc_len = ctx
            .file_len
            .checked_sub(ctx.toc_offset)
            .filter(|&v| v >= 9)
            .ok_or_else(|| IfixError::format("TOC_STRUCTURE", "TOC region missing or too short"))?;

        let mut tail_fixed = [0u8; 9];
        s.read_at(&mut tail_fixed, ctx.file_len - 9)?;
        let level_count = u32::from_le_bytes(tail_fixed[0..4].try_into().unwrap()) as usize;
        let fanout_byte = tail_fixed[4];
        if tail_fixed[5..9] != TEND {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "missing TEND tail marker",
            ));
        }
        if !(5..=16).contains(&fanout_byte) {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                format!("fanout exponent {fanout_byte} outside 5..=16"),
            ));
        }
        if fanout_byte != ctx.header_log2_page {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "tail fanout disagrees with header log2_page",
            ));
        }
        if level_count == 0 {
            return Err(IfixError::format("TOC_STRUCTURE", "level count is zero"));
        }

        let table_bytes = (level_count as u64)
            .checked_mul(8)
            .ok_or_else(|| IfixError::format("TOC_STRUCTURE", "level table size overflow"))?;
        if table_bytes + 9 > toc_len {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "declared level table runs past the TOC region",
            ));
        }
        let table_start = ctx.file_len - 9 - table_bytes;
        let mut raw = vec![0u8; table_bytes as usize];
        s.read_at(&mut raw, table_start)?;
        let level_lens: Vec<u64> = (0..level_count)
            .map(|i| u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap()))
            .collect();

        let lens_sum: u64 = level_lens
            .iter()
            .try_fold(0u64, |acc, &v| acc.checked_add(v))
            .ok_or_else(|| IfixError::format("TOC_STRUCTURE", "level lengths overflow"))?;
        if lens_sum + table_bytes + 9 != toc_len {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "TOC levels + tail do not exactly tile the TOC region",
            ));
        }

        let fanout = 1u32 << fanout_byte;
        let mut level_start = Vec::with_capacity(level_count);
        let mut level_entries = Vec::with_capacity(level_count);
        let mut cursor = ctx.toc_offset;
        for (i, &len) in level_lens.iter().enumerate() {
            if len == 0 || len % ENTRY != 0 {
                return Err(IfixError::format(
                    "TOC_STRUCTURE",
                    format!("level {i} has invalid length {len}"),
                ));
            }
            if !range_within(cursor, len, table_start) {
                return Err(IfixError::format(
                    "TOC_STRUCTURE",
                    format!("level {i} overruns the level table"),
                ));
            }
            level_start.push(cursor);
            level_entries.push(len / ENTRY);
            cursor += len;
        }

        let toc = Toc {
            fanout,
            level_start,
            level_entries,
        };
        toc.validate_counts(ctx)?;
        toc.validate_entries(s, ctx)?;
        Ok(toc)
    }

    /// Check level entry counts against each other and `node_count`.
    fn validate_counts(&self, ctx: TocContext) -> Result<()> {
        let f = self.fanout as u64;
        let leaf = self.level_entries[0];
        if leaf != ctx.node_count as u64 {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                format!("leaf entries {leaf} != node_count {}", ctx.node_count),
            ));
        }
        let mut expected = leaf;
        for (i, &n) in self.level_entries.iter().enumerate() {
            if i > 0 {
                let want = expected.div_ceil(f);
                if n != want {
                    return Err(IfixError::format(
                        "TOC_STRUCTURE",
                        format!("level {i} has {n} entries, expected {want}"),
                    ));
                }
                expected = n;
            }
        }
        if *self.level_entries.last().unwrap() != 1 {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "root level must hold one entry",
            ));
        }
        Ok(())
    }

    /// Stream every entry once, page by page, validating keys and pointers.
    fn validate_entries<S: Storage>(&self, s: &S, ctx: TocContext) -> Result<()> {
        // Leaf level: entry i has key i and record offset nodes_offset + i*48.
        let leaf_n = self.level_entries[0] as usize;
        let mut index = 0usize;
        while index < leaf_n {
            let page = self.read_page(s, 0, index)?;
            for (within, raw) in page.entries.iter().enumerate() {
                let idx = index + within;
                let key = u32::from_le_bytes(raw[0..4].try_into().unwrap());
                let payload = u64::from_le_bytes(raw[4..12].try_into().unwrap());
                if key != idx as u32 {
                    return Err(IfixError::format(
                        "TOC_STRUCTURE",
                        format!("leaf entry {idx} has key {key}"),
                    ));
                }
                let want = ctx.nodes_offset + (idx as u64) * 48;
                if payload != want {
                    return Err(IfixError::format(
                        "TOC_POINTER",
                        format!("leaf entry {idx} payload {payload} != record offset {want}"),
                    ));
                }
            }
            index += page.entries.len();
        }

        // Internal levels: entry j has key j*F^level and points at child
        // entry j*F (a page boundary).
        for level in 1..self.level_start.len() {
            let n = self.level_entries[level] as usize;
            let child_n = self.level_entries[level - 1];
            let child_start = self.level_start[level - 1];
            let child_end = child_start + child_n * ENTRY;
            let key_stride = (self.fanout as u64)
                .checked_pow(level as u32)
                .ok_or_else(|| IfixError::format("TOC_STRUCTURE", "key stride overflow"))?;
            let mut j = 0usize;
            while j < n {
                let page = self.read_page(s, level, j)?;
                for (within, raw) in page.entries.iter().enumerate() {
                    let jj = j + within;
                    let key = u32::from_le_bytes(raw[0..4].try_into().unwrap());
                    let child_entry = u64::from_le_bytes(raw[4..12].try_into().unwrap());
                    let want_key = (jj as u64 * key_stride) as u32;
                    if key != want_key {
                        return Err(IfixError::format(
                            "TOC_STRUCTURE",
                            format!("level {level} entry {jj} key {key} != {want_key}"),
                        ));
                    }
                    let want_ptr = child_start + (jj as u64) * self.fanout as u64 * ENTRY;
                    if child_entry != want_ptr {
                        return Err(IfixError::format(
                            "TOC_POINTER",
                            format!("level {level} entry {jj} pointer {child_entry} != {want_ptr}"),
                        ));
                    }
                    if child_entry
                        .checked_add(ENTRY)
                        .map(|e| e > child_end)
                        .unwrap_or(true)
                    {
                        return Err(IfixError::format(
                            "TOC_POINTER",
                            format!("level {level} entry {jj} points outside child level"),
                        ));
                    }
                }
                j += page.entries.len();
            }
        }

        // Root entry: key 0. Its payload points at the level below's first
        // page (or the first node record when the tree has a single level).
        let root_level = self.level_start.len() - 1;
        let mut root = [0u8; 12];
        s.read_at(&mut root, self.level_start[root_level])?;
        if u32::from_le_bytes(root[0..4].try_into().unwrap()) != 0 {
            return Err(IfixError::format(
                "TOC_STRUCTURE",
                "root entry key is not 0",
            ));
        }
        let want_root_payload = if root_level == 0 {
            ctx.nodes_offset
        } else {
            self.level_start[root_level - 1]
        };
        if u64::from_le_bytes(root[4..12].try_into().unwrap()) != want_root_payload {
            return Err(IfixError::format(
                "TOC_POINTER",
                "root entry does not point at the level below",
            ));
        }
        Ok(())
    }

    /// Read the page of `level` whose first entry has global index
    /// `start_index` (must be page-aligned).
    fn read_page<S: Storage>(&self, s: &S, level: usize, start_index: usize) -> Result<Page> {
        let f = self.fanout as usize;
        if start_index % f != 0 {
            return Err(IfixError::format(
                "TOC_POINTER",
                "internal: unaligned page request",
            ));
        }
        let n = self.level_entries[level] as usize;
        if start_index >= n {
            return Err(IfixError::format(
                "TOC_POINTER",
                "page request past level end",
            ));
        }
        let count = core::cmp::min(f, n - start_index);
        let offset = self.level_start[level] + (start_index as u64) * ENTRY;
        let mut buf = vec![0u8; count * ENTRY as usize];
        s.read_at(&mut buf, offset)?;
        Ok(Page {
            base_index: start_index,
            entries: buf
                .chunks_exact(ENTRY as usize)
                .map(|c| c.try_into().unwrap())
                .collect(),
        })
    }

    /// Validate that `ptr` is a page-aligned entry address inside `level`.
    fn check_page_pointer(&self, level: usize, ptr: u64) -> Result<usize> {
        let start = self.level_start[level];
        let end = start + self.level_entries[level] * ENTRY;
        if ptr < start || ptr >= end || (ptr - start) % ENTRY != 0 {
            return Err(IfixError::format(
                "TOC_POINTER",
                "followed pointer outside its target level",
            ));
        }
        let index = ((ptr - start) / ENTRY) as usize;
        if index % self.fanout as usize != 0 {
            return Err(IfixError::format(
                "TOC_POINTER",
                "followed pointer is not page-aligned",
            ));
        }
        Ok(index)
    }

    fn read_page_at<S: Storage>(&self, s: &S, level: usize, ptr: u64) -> Result<Page> {
        let index = self.check_page_pointer(level, ptr)?;
        self.read_page(s, level, index)
    }

    /// Navigate root -> leaf for node id `id`, returning its record offset.
    /// Touches exactly one page per level (plus the record, by the caller).
    pub fn resolve<S: Storage>(&self, s: &S, id: u32, node_count: u32) -> Result<u64> {
        if id >= node_count {
            return Err(IfixError::format(
                "BAD_NODE_ID",
                format!("node id {id} >= node_count {node_count}"),
            ));
        }
        let f = self.fanout as usize;
        let root_level = self.level_start.len() - 1;

        // Single-level TOC: locate the leaf page arithmetically.
        if root_level == 0 {
            let page = self.read_page(s, 0, (id as usize / f) * f)?;
            return self.leaf_payload(&page, id);
        }

        let mut root = [0u8; 12];
        s.read_at(&mut root, self.level_start[root_level])?;
        let mut ptr = u64::from_le_bytes(root[4..12].try_into().unwrap());

        // Walk from the level just below the root down to the leaves.
        let mut level = root_level - 1;
        loop {
            let page = self.read_page_at(s, level, ptr)?;
            // At level L the covering entry has index j = floor(id / F^L).
            let stride = (self.fanout as u64)
                .checked_pow(level as u32)
                .ok_or_else(|| IfixError::format("TOC_STRUCTURE", "level stride overflow"))?
                as usize;
            let j = id as usize / stride;
            let within = j
                .checked_sub(page.base_index)
                .filter(|&w| w < page.entries.len())
                .ok_or_else(|| {
                    IfixError::format("TOC_POINTER", "id not covered by the followed page")
                })?;
            if page.key(within) != (j * stride) as u32 {
                return Err(IfixError::format(
                    "TOC_POINTER",
                    "followed entry key disagrees with its position",
                ));
            }
            if level == 0 {
                // Leaf page: payload is the node-record offset.
                return Ok(page.payload(within));
            }
            ptr = page.payload(within);
            level -= 1;
        }
    }

    fn leaf_payload(&self, page: &Page, id: u32) -> Result<u64> {
        let within = (id as usize)
            .checked_sub(page.base_index)
            .filter(|&w| w < page.entries.len())
            .ok_or_else(|| IfixError::format("TOC_POINTER", "id not covered by leaf page"))?;
        if page.key(within) != id {
            return Err(IfixError::format(
                "TOC_POINTER",
                format!(
                    "leaf page contains key {} where {id} expected",
                    page.key(within)
                ),
            ));
        }
        Ok(page.payload(within))
    }

    pub fn height(&self) -> usize {
        self.level_start.len()
    }
}
