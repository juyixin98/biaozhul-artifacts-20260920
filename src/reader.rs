//! Typed readers over IFIX files.
//!
//! Streaming-file and memory-mapped byte sources (see [`crate::source`])
//! share one decode engine implemented here.
//!
//! Every record is validated *as it is read*. The decoder therefore never
//! trusts an offset, count or interval enough to allocate from it: ranges use
//! checked arithmetic, live inside validated sections, and every decoded
//! length is capped by [`Limits`].

use std::io::Write;
use std::path::Path;

use crate::error::{Error, Result};
use crate::format::{
    read_u32, Header, Limits, NodeType, BLOCK_SIZE, ENTRY_SIZE, FANOUT, HEADER_SIZE, KIND_INTERNAL,
    KIND_LEAF, NIL, NODE_SIZE,
};
use crate::source::{FileSource, MmapSource, Source};
// Re-exported so callers can use `ifix::reader::SliceSource` and the crate
// root re-export interchangeably.
pub use crate::source::SliceSource;

/// Empty index-entry marker.
const ENTRY_EMPTY: u64 = u64::MAX;

/// A parsed, fully field-validated node record.
#[derive(Debug, Clone)]
pub struct NodeInfo {
    /// Node index in the nodes section.
    pub id: u32,
    /// Node kind.
    pub kind: NodeType,
    /// Byte range of the name inside the names section.
    pub name: (u64, u64),
    /// Byte range of payload inside the blob section (files/symlinks only).
    pub data: (u64, u64),
    /// Root block of this directory's child index, or [`NIL`].
    pub first_block: u32,
    /// Declared number of immediate children.
    pub child_count: u32,
    /// Stored mode bits.
    pub mode: u64,
    /// Stored modification time (unix seconds).
    pub mtime: u64,
}

/// One directory entry as returned by listings.
#[derive(Debug, Clone)]
pub struct DirEntry {
    /// Base name (no slash).
    pub name: String,
    /// Child node id.
    pub node_id: u32,
    /// Child kind.
    pub kind: NodeType,
    /// Payload length in the blob section.
    pub size: u64,
}

/// One parsed index entry (in-memory, transient — never an indexed cache).
#[derive(Clone, Copy)]
pub(crate) struct Entry {
    key_off: u64,
    key_len: u64,
    child: u64,
}

/// A raw index entry exposed to validation/tests: `(key_off, key_len, child)`.
pub type RawEntry = (u64, u64, u64);

/// The shared decode engine.
pub struct Reader {
    src: Box<dyn Source>,
    header: Header,
    limits: Limits,
}

impl Reader {
    /// Construct over any [`Source`] after validating the superblock.
    ///
    /// Only the 128-byte header is read here; no section content is touched
    /// and nothing is allocated according to declared counts.
    pub fn new(src: Box<dyn Source>, limits: Limits) -> Result<Self> {
        let file_len = src.len();
        if file_len < HEADER_SIZE {
            return Err(Error::TruncatedHeader);
        }
        let mut hbuf = [0u8; HEADER_SIZE as usize];
        src.read_at(&mut hbuf, 0)?;
        let header = Limits::parse_header(&hbuf, file_len)?;
        limits.check_caps(&header)?;
        Ok(Reader {
            src,
            header,
            limits,
        })
    }

    /// Streaming file reader.
    pub fn open_file<P: AsRef<Path>>(path: P, limits: Limits) -> Result<Self> {
        Reader::new(Box::new(FileSource::open(path)?), limits)
    }

    /// Memory-mapped reference reader.
    pub fn open_mmap<P: AsRef<Path>>(path: P, limits: Limits) -> Result<Self> {
        Reader::new(Box::new(MmapSource::open(path)?), limits)
    }

    /// Validated superblock.
    pub fn header(&self) -> &Header {
        &self.header
    }

    /// Configured limits.
    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    // ------------------------------------------------------------ raw fetches

    /// Read and fully validate one node record.
    pub fn load_node(&self, id: u32) -> Result<NodeInfo> {
        if id as u64 >= self.header.node_count as u64 {
            return Err(Error::BadNodeRef {
                index: id as u64,
                count: self.header.node_count as u64,
            });
        }
        let at = self.header.nodes.0 + (id as u64) * NODE_SIZE;
        let mut buf = [0u8; NODE_SIZE as usize];
        self.src.read_at(&mut buf, at)?;
        self.parse_node(id, &buf)
    }

    fn parse_node(&self, id: u32, b: &[u8; NODE_SIZE as usize]) -> Result<NodeInfo> {
        let kind = NodeType::from_byte(b[0])?;
        for &x in &b[1..4] {
            if x != 0 {
                return Err(Error::BadNodeRecord {
                    index: id,
                    detail: "reserved padding bytes nonzero",
                });
            }
        }
        let name_off = read_u32(b, 4) as u64;
        let name_len = read_u32(b, 8) as u64;
        let data_off = u64::from_le_bytes(b[16..24].try_into().unwrap());
        let data_len = u64::from_le_bytes(b[24..32].try_into().unwrap());
        let first_block = read_u32(b, 32);
        let child_count = read_u32(b, 36);
        let mode = u64::from_le_bytes(b[40..48].try_into().unwrap());
        let mtime = u64::from_le_bytes(b[48..56].try_into().unwrap());
        for &x in &b[56..64] {
            if x != 0 {
                return Err(Error::BadNodeRecord {
                    index: id,
                    detail: "reserved tail bytes nonzero",
                });
            }
        }

        // Name interval must live inside the names section (checked add).
        let (ns, ne) = self.header.names;
        let nbound = ne - ns;
        let name_end = name_off.checked_add(name_len).ok_or(Error::OutOfRange {
            what: "name",
            start: name_off,
            len: name_len,
            bound: nbound,
        })?;
        if name_end > nbound {
            return Err(Error::OutOfRange {
                what: "name",
                start: name_off,
                len: name_len,
                bound: nbound,
            });
        }
        if name_len > self.limits.max_name_len {
            return Err(Error::LimitExceeded {
                what: "name_len",
                limit: self.limits.max_name_len,
            });
        }
        // Root needs no name; every other node's name is nonempty.
        if id != self.header.root_node && name_len == 0 {
            return Err(Error::BadName(id));
        }

        // Blob interval for payload-bearing kinds.
        let (bs, be) = self.header.blobs;
        let bbound = be - bs;
        match kind {
            NodeType::Dir => {
                if data_off != 0 || data_len != 0 {
                    return Err(Error::BadNodeRecord {
                        index: id,
                        detail: "directory carries blob payload",
                    });
                }
            }
            NodeType::File | NodeType::Symlink => {
                let end = data_off.checked_add(data_len).ok_or(Error::OutOfRange {
                    what: "blob",
                    start: data_off,
                    len: data_len,
                    bound: bbound,
                })?;
                if end > bbound {
                    return Err(Error::OutOfRange {
                        what: "blob",
                        start: data_off,
                        len: data_len,
                        bound: bbound,
                    });
                }
                // A symlink target is a nonempty byte string (FORMAT §3).
                if kind == NodeType::Symlink && data_len == 0 {
                    return Err(Error::BadNodeRecord {
                        index: id,
                        detail: "symlink target is empty",
                    });
                }
            }
        }

        // Index linkage.
        match kind {
            NodeType::Dir => {
                if first_block == NIL {
                    if child_count != 0 {
                        return Err(Error::BadNodeRecord {
                            index: id,
                            detail: "child_count>0 but first_block is NIL",
                        });
                    }
                } else {
                    if first_block as u64 >= self.header.block_count as u64 {
                        return Err(Error::BadBlockRef {
                            index: first_block as u64,
                            count: self.header.block_count as u64,
                        });
                    }
                    if child_count == 0 {
                        return Err(Error::BadNodeRecord {
                            index: id,
                            detail: "first_block set but child_count is 0",
                        });
                    }
                }
            }
            NodeType::File | NodeType::Symlink => {
                if first_block != NIL || child_count != 0 {
                    return Err(Error::BadNodeRecord {
                        index: id,
                        detail: "non-directory carries child index",
                    });
                }
            }
        }

        Ok(NodeInfo {
            id,
            kind,
            name: (name_off, name_len),
            data: (data_off, data_len),
            first_block,
            child_count,
            mode,
            mtime,
        })
    }

    /// Fetch one index block, parse its 16 entries and validate kind, padding,
    /// packed-prefix layout, key ordering and every child reference.
    pub(crate) fn load_block(&self, idx: u32) -> Result<(u8, Vec<Entry>)> {
        if idx as u64 >= self.header.block_count as u64 {
            return Err(Error::BadBlockRef {
                index: idx as u64,
                count: self.header.block_count as u64,
            });
        }
        let at = self.header.blocks.0 + (idx as u64) * BLOCK_SIZE;
        let mut buf = vec![0u8; BLOCK_SIZE as usize];
        self.src.read_at(&mut buf, at)?;

        let kind = buf[0];
        if kind != KIND_LEAF && kind != KIND_INTERNAL {
            return Err(Error::BadBlockKind(kind));
        }
        for &x in &buf[1..4] {
            if x != 0 {
                return Err(Error::CorruptIndex {
                    detail: "block reserved padding nonzero",
                });
            }
        }

        let (ns, ne) = self.header.names;
        let nbound = ne - ns;
        let mut entries = Vec::with_capacity(FANOUT);
        let mut seen_empty = false;
        let mut prev: Option<Vec<u8>> = None;
        for slot in 0..FANOUT {
            let base = 4 + slot * ENTRY_SIZE;
            let key_off = read_u32(&buf, base) as u64;
            let key_len = read_u32(&buf, base + 4) as u64;
            let child = u64::from_le_bytes(buf[base + 8..base + 16].try_into().unwrap());

            if child == ENTRY_EMPTY {
                seen_empty = true;
                continue;
            }
            if seen_empty {
                return Err(Error::CorruptIndex {
                    detail: "active entry after empty slot (not packed)",
                });
            }

            // Key interval check.
            let key_end = key_off.checked_add(key_len).ok_or(Error::OutOfRange {
                what: "index key",
                start: key_off,
                len: key_len,
                bound: nbound,
            })?;
            if key_end > nbound || key_len == 0 || key_len > self.limits.max_name_len {
                return Err(Error::CorruptIndex {
                    detail: "entry key outside names section or invalid length",
                });
            }

            // Child reference shape per block kind.
            if kind == KIND_INTERNAL {
                if child >= self.header.block_count as u64 {
                    return Err(Error::BadBlockRef {
                        index: child,
                        count: self.header.block_count as u64,
                    });
                }
                // Acyclic multi-level index: every child block has a strictly
                // greater index than its parent (blocks are emitted postorder).
                if child as u32 <= idx {
                    return Err(Error::NonForwardBlockRef {
                        parent: idx,
                        child: child as u32,
                    });
                }
            } else if child >= self.header.node_count as u64 {
                return Err(Error::BadNodeRef {
                    index: child,
                    count: self.header.node_count as u64,
                });
            }

            // Strict key ordering (bytes are fetched from the names section).
            let key = self.read_names(key_off, key_len)?;
            if let Some(p) = &prev {
                if p.as_slice() >= key.as_slice() {
                    return Err(Error::BadOrdering("index keys not strictly ascending"));
                }
            }
            prev = Some(key);

            entries.push(Entry {
                key_off,
                key_len,
                child,
            });
        }
        if entries.is_empty() {
            return Err(Error::CorruptIndex {
                detail: "block has no active entries",
            });
        }
        Ok((kind, entries))
    }

    fn read_names(&self, off: u64, len: u64) -> Result<Vec<u8>> {
        if len > self.limits.max_name_len {
            return Err(Error::LimitExceeded {
                what: "name_len",
                limit: self.limits.max_name_len,
            });
        }
        let mut out = vec![0u8; len as usize];
        let at = self.header.names.0 + off;
        self.src.read_at(&mut out, at)?;
        Ok(out)
    }

    fn read_blob(&self, off: u64, len: u64) -> Result<Vec<u8>> {
        if len > self.limits.max_output {
            return Err(Error::LimitExceeded {
                what: "output",
                limit: self.limits.max_output,
            });
        }
        let mut out = vec![0u8; len as usize];
        let at = self.header.blobs.0 + off;
        self.src.read_at(&mut out, at)?;
        Ok(out)
    }

    /// Materialize a node's name as UTF-8.
    pub fn node_name(&self, node: &NodeInfo) -> Result<String> {
        let raw = self.read_names(node.name.0, node.name.1)?;
        String::from_utf8(raw).map_err(|_| Error::BadNameEncoding(node.id))
    }

    // ----------------------------------------------------------- tree lookup

    /// Root node (always a directory in a well-formed file).
    pub fn root(&self) -> Result<NodeInfo> {
        let n = self.load_node(self.header.root_node)?;
        if n.kind != NodeType::Dir {
            return Err(Error::BadNodeRecord {
                index: n.id,
                detail: "root node is not a directory",
            });
        }
        Ok(n)
    }

    /// Compare entry key with `want`, fetching the key bytes.
    fn entry_key(&self, e: Entry) -> Result<Vec<u8>> {
        self.read_names(e.key_off, e.key_len)
    }

    /// Descend one directory's index looking for an exact child name.
    ///
    /// Touches O(tree height) blocks and O(height * log FANOUT) names — never
    /// sibling subtrees and never the node section beyond the one result.
    fn index_find(&self, root_block: u32, want: &[u8]) -> Result<Option<u32>> {
        let mut block = root_block;
        let mut hops = 0u64;
        loop {
            hops += 1;
            if hops > self.header.block_count as u64 {
                return Err(Error::CorruptIndex {
                    detail: "block chain longer than block_count",
                });
            }
            let (kind, entries) = self.load_block(block)?;
            // Lower-bound binary search: lo is the first entry whose key >= want.
            let mut lo = 0usize;
            let mut hi = entries.len();
            while lo < hi {
                let mid = (lo + hi) / 2;
                let key = self.entry_key(entries[mid])?;
                if key.as_slice() < want {
                    lo = mid + 1;
                } else {
                    hi = mid;
                }
            }
            match kind {
                KIND_LEAF => {
                    if lo < entries.len() {
                        let key = self.entry_key(entries[lo])?;
                        if key.as_slice() == want {
                            return Ok(Some(entries[lo].child as u32));
                        }
                    }
                    return Ok(None);
                }
                KIND_INTERNAL => {
                    // Rightmost subtree whose first key <= want: if lo points
                    // at an exact/larger boundary we step left when greater.
                    let mut choice = lo;
                    if choice >= entries.len() {
                        choice = entries.len() - 1;
                    } else {
                        let key = self.entry_key(entries[choice])?;
                        if key.as_slice() > want && choice > 0 {
                            choice -= 1;
                        }
                    }
                    let next = entries[choice].child as u32;
                    // Strictly increasing block ids terminate the descent.
                    if next <= block {
                        return Err(Error::NonForwardBlockRef {
                            parent: block,
                            child: next,
                        });
                    }
                    block = next;
                }
                _ => unreachable!("kind validated in load_block"),
            }
        }
    }

    /// Split a slash-separated path into bounded components.
    fn split_path(path: &str, limits: Limits) -> Result<Vec<String>> {
        let mut out = Vec::new();
        for part in path.split('/') {
            if part.is_empty() || part == "." {
                continue;
            }
            if part.contains('\0') {
                return Err(Error::BadName(u32::MAX));
            }
            if out.len() as u32 >= limits.max_depth {
                return Err(Error::LimitExceeded {
                    what: "path_depth",
                    limit: limits.max_depth as u64,
                });
            }
            out.push(part.to_owned());
        }
        Ok(out)
    }

    /// Resolve a `/`-separated path, following symlinks, returning the node.
    ///
    /// * `.` components and repeated/leading/trailing slashes are ignored,
    /// * `..` pops one physically-descended directory (symlinks are not
    ///   counted as physical descent, matching POSIX),
    /// * absolute symlink targets restart at the root,
    /// * `follow_final=false` reproduces `lstat` semantics for the last
    ///   component.
    pub fn lookup(&self, path: &str, follow_final: bool) -> Result<NodeInfo> {
        let mut cur = self.root()?;
        // Directory ids physically descended through (for `..`).
        let mut dirstack: Vec<u32> = Vec::new();
        let mut components = Self::split_path(path, self.limits)?;
        // Symlink node ids already encountered in this resolution: a repeat
        // proves a cycle; the depth cap bounds resolution time regardless.
        let mut seen_links: std::collections::HashSet<u32> = std::collections::HashSet::new();
        let mut link_depth = 0u32;

        loop {
            if components.is_empty() {
                return Ok(cur);
            }
            let component = components.remove(0);

            if component == ".." {
                cur = match dirstack.pop() {
                    Some(id) => self.load_node(id)?,
                    None => self.root()?,
                };
                continue;
            }

            if cur.kind != NodeType::Dir {
                return Err(Error::NotADirectory(component));
            }
            let child_id = match cur.first_block {
                NIL => None,
                blk => self.index_find(blk, component.as_bytes())?,
            };
            let child = match child_id {
                Some(id) => self.load_node(id)?,
                None => return Err(Error::NotFound(component)),
            };
            let is_final = components.is_empty();

            if child.kind == NodeType::Symlink && (!is_final || follow_final) {
                link_depth += 1;
                if link_depth > self.limits.max_link_depth {
                    return Err(Error::LinkCycleOrDepth(path.to_owned()));
                }
                if !seen_links.insert(child.id) {
                    return Err(Error::LinkCycleOrDepth(path.to_owned()));
                }
                let target = self.read_blob(child.data.0, child.data.1)?;
                let target =
                    String::from_utf8(target).map_err(|_| Error::BadNameEncoding(child.id))?;
                let parts = Self::split_path(&target, self.limits)?;
                if target.starts_with('/') {
                    cur = self.root()?;
                    dirstack.clear();
                }
                // Relative target: cur stays the containing directory. Splice
                // the target's components in front of the remaining path.
                let mut next = parts;
                next.append(&mut components);
                components = next;
                continue;
            }

            if child.kind == NodeType::Dir {
                dirstack.push(cur.id);
            }
            cur = child;
        }
    }

    /// List a directory's immediate children in index order, capped by
    /// [`Limits::max_listing`].
    pub fn list(&self, path: &str) -> Result<Vec<DirEntry>> {
        let dir = self.lookup(path, true)?;
        if dir.kind != NodeType::Dir {
            return Err(Error::NotADirectory(path.to_owned()));
        }
        let mut out = Vec::new();
        if dir.first_block == NIL {
            return Ok(out);
        }
        // Iterative DFS; internal blocks always point to higher block ids.
        let mut stack: Vec<(u32, usize)> = vec![(dir.first_block, 0)];
        let mut guard = 0u64;
        while let Some((block_idx, slot)) = stack.last_mut() {
            guard += 1;
            if guard > self.header.block_count as u64 * (FANOUT as u64 + 1) + 1 {
                return Err(Error::CorruptIndex {
                    detail: "listing traversal exceeded block bound",
                });
            }
            let (kind, entries) = self.load_block(*block_idx)?;
            if *slot >= entries.len() {
                stack.pop();
                continue;
            }
            let e = entries[*slot];
            *slot += 1;
            match kind {
                KIND_INTERNAL => {
                    // The parent frame stays beneath this one on the stack and
                    // resumes at the next slot only once this subtree is done,
                    // so subtrees are visited in ascending key order.
                    stack.push((e.child as u32, 0));
                }
                KIND_LEAF => {
                    if out.len() as u64 >= self.limits.max_listing {
                        return Err(Error::LimitExceeded {
                            what: "listing",
                            limit: self.limits.max_listing,
                        });
                    }
                    let node = self.load_node(e.child as u32)?;
                    let name = self.node_name(&node)?;
                    out.push(DirEntry {
                        name,
                        node_id: node.id,
                        kind: node.kind,
                        size: node.data.1,
                    });
                }
                _ => unreachable!(),
            }
        }
        if out.len() as u32 != dir.child_count {
            return Err(Error::CorruptIndex {
                detail: "leaf count does not match directory child_count",
            });
        }
        Ok(out)
    }

    /// Stream a file node's payload to `out`, honoring `offset`/`len` and the
    /// global output cap. Returns bytes copied. Uses a fixed 16 KiB buffer.
    pub fn copy_file(
        &self,
        node: &NodeInfo,
        out: &mut dyn Write,
        offset: u64,
        len: Option<u64>,
    ) -> Result<u64> {
        if node.kind != NodeType::File {
            return Err(Error::BadNodeRecord {
                index: node.id,
                detail: "node is not a regular file",
            });
        }
        if offset > node.data.1 {
            return Err(Error::OutOfRange {
                what: "read offset",
                start: offset,
                len: 0,
                bound: node.data.1,
            });
        }
        let want = len
            .unwrap_or(node.data.1 - offset)
            .min(node.data.1 - offset);
        if want > self.limits.max_output {
            return Err(Error::LimitExceeded {
                what: "output",
                limit: self.limits.max_output,
            });
        }
        let mut remaining = want;
        let mut chunk = vec![0u8; 16 * 1024];
        let base = self.header.blobs.0 + node.data.0 + offset;
        let mut pos = 0u64;
        while remaining > 0 {
            let take = remaining.min(chunk.len() as u64) as usize;
            self.src.read_at(&mut chunk[..take], base + pos)?;
            out.write_all(&chunk[..take])?;
            pos += take as u64;
            remaining -= take as u64;
        }
        Ok(want)
    }

    /// Read a file node's whole payload (capped by [`Limits::max_output`]).
    pub fn read_file(&self, node: &NodeInfo) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        self.copy_file(node, &mut out, 0, None)?;
        Ok(out)
    }

    /// Read a symlink target string.
    pub fn symlink_target(&self, node: &NodeInfo) -> Result<String> {
        if node.kind != NodeType::Symlink {
            return Err(Error::BadNodeRecord {
                index: node.id,
                detail: "node is not a symlink",
            });
        }
        let raw = self.read_blob(node.data.0, node.data.1)?;
        String::from_utf8(raw).map_err(|_| Error::BadNameEncoding(node.id))
    }

    /// Block accessor used by the whole-file validator and by integration
    /// tests. Returns `(kind, entries)` where each entry is
    /// `(key_off, key_len, child)`.
    pub fn debug_block(&self, idx: u32) -> Result<(u8, Vec<RawEntry>)> {
        let (kind, entries) = self.load_block(idx)?;
        Ok((
            kind,
            entries
                .iter()
                .map(|e| (e.key_off, e.key_len, e.child))
                .collect(),
        ))
    }

    /// Raw name-section bytes by interval (crate-internal).
    pub(crate) fn read_name_raw(&self, off: u64, len: u64) -> Result<Vec<u8>> {
        self.read_names(off, len)
    }
}
