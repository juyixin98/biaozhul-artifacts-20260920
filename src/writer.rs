//! Streaming-friendly encoder for IFIX.
//!
//! The writer is the only place that trusts its input: it parses a JSON tree
//! document, validates it, assigns node ids in preorder, packs name and blob
//! intervals without gaps or overlap, and constructs every directory index as
//! a postorder B+-style tree of [`FANOUT`]-ary blocks (so every internal block
//! references only strictly higher block indices — the property the decoder
//! relies on for acyclicity).

use crate::base64;
use crate::error::{Error, Result};
use crate::format::{
    Limits, NodeType, BLOCK_SIZE, ENTRY_SIZE, FANOUT, FORMAT_VERSION, HEADER_SIZE, KIND_INTERNAL,
    KIND_LEAF, MAGIC, NIL, NODE_SIZE,
};
use crate::json::Value;

/// Options governing a build (currently just the resource caps).
#[derive(Debug, Clone, Copy, Default)]
pub struct WriteOptions {
    /// Caps to enforce on the produced file.
    pub limits: Limits,
}

/// Intermediate node while the tree is being assembled.
struct NodeSpec {
    name: String,
    kind: NodeType,
    mode: u64,
    mtime: u64,
    data: Vec<u8>,
    children: Vec<u32>,
}

/// One index block under construction, using *local* child references.
#[derive(Clone)]
enum LocalChild {
    /// Leaf entry: child node id.
    Node(u32),
    /// Internal entry: local index of the child block in `LocalTree.blocks`.
    Block(usize),
}

/// A block before global numbering. Keys are stored as the leftmost child's
/// node id; the on-disk key interval is resolved from that node's name.
#[derive(Clone)]
struct LocalBlock {
    kind: u8,
    /// Entries `(leftmost_node_id, child)`, ordered ascending by that name.
    entries: Vec<(u32, LocalChild)>,
}

/// One directory's independently-built tree.
struct LocalTree {
    blocks: Vec<LocalBlock>,
    /// Local index of the tree's root block.
    root: usize,
}

/// Accumulator for the whole build.
struct Builder {
    nodes: Vec<NodeSpec>,
    /// Name interval per node, relative to the names section.
    name_iv: Vec<(u32, u32)>,
    /// Blob interval per node, relative to the blob section.
    data_iv: Vec<(u64, u64)>,
    /// First index block per directory.
    first_block: Vec<u32>,
    blocks: Vec<BlockData>,
    names: Vec<u8>,
    blobs: Vec<u8>,
    limits: Limits,
}

/// Final, serializable index block.
struct BlockData {
    kind: u8,
    /// Packed, ordered entries: `(key_name_off, key_name_len, child)`.
    entries: Vec<(u32, u32, u64)>,
}

impl Builder {
    fn new(limits: Limits) -> Self {
        Builder {
            nodes: Vec::new(),
            name_iv: Vec::new(),
            data_iv: Vec::new(),
            first_block: Vec::new(),
            blocks: Vec::new(),
            names: Vec::new(),
            blobs: Vec::new(),
            limits,
        }
    }

    fn add_node(&mut self, mut spec: NodeSpec, is_root: bool) -> Result<u32> {
        validate_name(&spec.name, is_root)?;
        if spec.name.len() as u64 > self.limits.max_name_len {
            return Err(Error::LimitExceeded {
                what: "name_len",
                limit: self.limits.max_name_len,
            });
        }

        // Name interval (packed, no overlap).
        let name_off = self.names.len() as u32;
        self.names.extend_from_slice(spec.name.as_bytes());
        let name_len = spec.name.len() as u32;

        // Blob interval for payload kinds.
        let data = std::mem::take(&mut spec.data);
        let data_iv = match spec.kind {
            NodeType::Dir => (0u64, 0u64),
            NodeType::File | NodeType::Symlink => {
                if data.len() as u64 > self.limits.max_output {
                    return Err(Error::LimitExceeded {
                        what: "blob_len",
                        limit: self.limits.max_output,
                    });
                }
                let off = self.blobs.len() as u64;
                self.blobs.extend_from_slice(&data);
                (off, data.len() as u64)
            }
        };

        let id = self.nodes.len() as u32;
        self.nodes.push(spec);
        self.name_iv.push((name_off, name_len));
        self.data_iv.push(data_iv);
        self.first_block.push(NIL);

        if self.nodes.len() as u64 > self.limits.max_nodes {
            return Err(Error::LimitExceeded {
                what: "node_count",
                limit: self.limits.max_nodes,
            });
        }
        Ok(id)
    }

    /// Recursively ingest a JSON node and its subtree.
    fn ingest(&mut self, value: &Value, is_root: bool) -> Result<u32> {
        value
            .as_object()
            .ok_or_else(|| Error::Json("node must be an object".to_string()))?;
        let name = field_str(value, "name")?.to_owned();
        let type_str = field_str(value, "type")?;
        let kind = match type_str {
            "file" => NodeType::File,
            "dir" => NodeType::Dir,
            "directory" => NodeType::Dir,
            "symlink" => NodeType::Symlink,
            other => return Err(Error::Json(format!("unknown node type {other:?}"))),
        };
        let mode = match value.get("mode") {
            Some(v) => v
                .as_i64()
                .ok_or(Error::Json("mode must be integer".into()))? as u64,
            None => default_mode(kind),
        };
        let mtime = match value.get("mtime") {
            Some(v) => v
                .as_i64()
                .ok_or(Error::Json("mtime must be integer".into()))?
                .max(0) as u64,
            None => 0,
        };

        let mut spec = NodeSpec {
            name,
            kind,
            mode,
            mtime,
            data: Vec::new(),
            children: Vec::new(),
        };

        match kind {
            NodeType::File => {
                match value.get("content_base64") {
                    Some(Value::Str(s)) => spec.data = base64::decode(s).map_err(Error::Json)?,
                    Some(_) => return Err(Error::Json("content_base64 must be a string".into())),
                    None => {}
                }
                reject_children(value)?;
            }
            NodeType::Symlink => {
                let target = field_str(value, "target")?;
                if target.is_empty() {
                    return Err(Error::Json("symlink target is empty".into()));
                }
                spec.data = target.as_bytes().to_vec();
                reject_children(value)?;
            }
            NodeType::Dir => {
                if let Some(children) = value.get("children") {
                    let arr = children
                        .as_array()
                        .ok_or_else(|| Error::Json("children must be an array".into()))?;
                    // Reserve this node first, then recurse so ids are preorder.
                    let id = self.add_node(spec, is_root)?;
                    let mut seen = std::collections::BTreeSet::new();
                    for child in arr {
                        let cid = self.ingest(child, false)?;
                        let cname = &self.nodes[cid as usize].name;
                        if !seen.insert(cname.clone()) {
                            return Err(Error::Json(format!(
                                "duplicate child name {cname:?} in directory"
                            )));
                        }
                        self.nodes[id as usize].children.push(cid);
                    }
                    return Ok(id);
                } else if is_root {
                    let id = self.add_node(spec, true)?;
                    return Ok(id);
                } else {
                    return Err(Error::Json("directory node missing children".into()));
                }
            }
        }
        self.add_node(spec, is_root)
    }

    /// Build every directory's index as a local postorder tree, then emit
    /// blocks preorder (root first) so each internal block references only
    /// strictly *higher* global block ids — the acyclicity rule the decoder
    /// enforces.
    fn build_indexes(&mut self) -> Result<()> {
        let dir_ids: Vec<u32> = self
            .nodes
            .iter()
            .enumerate()
            .filter(|(_, n)| n.kind == NodeType::Dir)
            .map(|(i, _)| i as u32)
            .collect();
        for dir_id in dir_ids {
            let mut children: Vec<u32> = self.nodes[dir_id as usize].children.clone();
            children.sort_by(|&a, &b| {
                self.nodes[a as usize]
                    .name
                    .as_bytes()
                    .cmp(self.nodes[b as usize].name.as_bytes())
            });

            if children.is_empty() {
                self.first_block[dir_id as usize] = NIL;
                continue;
            }

            let tree = Self::build_local_tree(&children)?;
            let root_block = self.emit_tree(&tree)?;
            self.first_block[dir_id as usize] = root_block;
        }
        Ok(())
    }

    /// Build one directory's tree locally.
    ///
    /// Level 0 is leaf blocks over `FANOUT` node children; higher levels are
    /// internal blocks over the previous level's blocks. Every key carried in
    /// an internal entry is the leftmost descendant node id, so on-disk key
    /// bytes are recovered from the node's name interval.
    fn build_local_tree(children: &[u32]) -> Result<LocalTree> {
        let mut blocks: Vec<LocalBlock> = Vec::new();

        // Leaf level.
        let mut level: Vec<(u32, LocalChild)> = Vec::new();
        for chunk in children.chunks(FANOUT) {
            let entries: Vec<(u32, LocalChild)> = chunk
                .iter()
                .map(|&cid| (cid, LocalChild::Node(cid)))
                .collect();
            let idx = blocks.len();
            blocks.push(LocalBlock {
                kind: KIND_LEAF,
                entries,
            });
            level.push((chunk[0], LocalChild::Block(idx)));
        }

        // Internal levels.
        while level.len() > 1 {
            let mut next: Vec<(u32, LocalChild)> = Vec::new();
            for chunk in level.chunks(FANOUT) {
                let idx = blocks.len();
                blocks.push(LocalBlock {
                    kind: KIND_INTERNAL,
                    entries: chunk.to_vec(),
                });
                next.push((chunk[0].0, LocalChild::Block(idx)));
            }
            level = next;
        }

        let root = match &level[0].1 {
            LocalChild::Block(i) => *i,
            LocalChild::Node(_) => unreachable!("root is always a block"),
        };
        Ok(LocalTree { blocks, root })
    }

    /// Append one local tree's blocks in preorder, translating local block
    /// indices to the global scheme, and return the global root id.
    fn emit_tree(&mut self, tree: &LocalTree) -> Result<u32> {
        // Preorder DFS assigns ids; emit order itself is irrelevant to the
        // property, but preorder keeps related blocks adjacent.
        let mut global_ids = vec![0u32; tree.blocks.len()];
        let mut order: Vec<usize> = Vec::with_capacity(tree.blocks.len());
        let mut stack = vec![tree.root];
        while let Some(li) = stack.pop() {
            order.push(li);
            let block = &tree.blocks[li];
            if block.kind == KIND_INTERNAL {
                // Push in reverse so the first child is popped first.
                for (_, child) in block.entries.iter().rev() {
                    if let LocalChild::Block(ci) = child {
                        stack.push(*ci);
                    }
                }
            }
        }
        for (seq, &li) in order.iter().enumerate() {
            global_ids[li] = (self.blocks.len() + seq) as u32;
        }

        for &li in &order {
            let local = &tree.blocks[li];
            let mut entries = Vec::with_capacity(local.entries.len());
            for (leftmost_node, child) in &local.entries {
                let (off, len) = self.name_iv[*leftmost_node as usize];
                let global_child: u64 = match child {
                    LocalChild::Node(nid) => *nid as u64,
                    LocalChild::Block(ci) => global_ids[*ci] as u64,
                };
                entries.push((off, len, global_child));
            }
            self.push_block(BlockData {
                kind: local.kind,
                entries,
            })?;
        }
        Ok(global_ids[tree.root])
    }

    fn push_block(&mut self, block: BlockData) -> Result<u32> {
        assert!(!block.entries.is_empty() && block.entries.len() <= FANOUT);
        let id = self.blocks.len() as u32;
        self.blocks.push(block);
        if self.blocks.len() as u64 > self.limits.max_blocks {
            return Err(Error::LimitExceeded {
                what: "block_count",
                limit: self.limits.max_blocks,
            });
        }
        Ok(id)
    }

    /// Serialize the complete file image.
    fn finish(self) -> Result<Vec<u8>> {
        let node_count = self.nodes.len() as u64;
        let block_count = self.blocks.len() as u64;
        let nodes_len = node_count * NODE_SIZE;
        let names_len = self.names.len() as u64;
        let blocks_len = block_count * BLOCK_SIZE;
        let blobs_len = self.blobs.len() as u64;

        let nodes_start = HEADER_SIZE;
        let names_start = nodes_start + nodes_len;
        let blocks_start = names_start + names_len;
        let blobs_start = blocks_start + blocks_len;
        let total = blobs_start + blobs_len;
        if total > self.limits.max_file_len {
            return Err(Error::LimitExceeded {
                what: "file_len",
                limit: self.limits.max_file_len,
            });
        }

        let mut out = Vec::with_capacity(total as usize);
        out.extend_from_slice(&self.serialize_header(
            nodes_start,
            names_start,
            blocks_start,
            blobs_start,
            total,
        ));
        self.serialize_nodes(&mut out);
        out.extend_from_slice(&self.names);
        self.serialize_blocks(&mut out);
        out.extend_from_slice(&self.blobs);
        debug_assert_eq!(out.len() as u64, total);
        Ok(out)
    }

    fn serialize_header(
        &self,
        nodes_start: u64,
        names_start: u64,
        blocks_start: u64,
        blobs_start: u64,
        total: u64,
    ) -> [u8; HEADER_SIZE as usize] {
        let mut h = [0u8; HEADER_SIZE as usize];
        h[0..8].copy_from_slice(&MAGIC);
        h[8] = FORMAT_VERSION;
        put_u64(&mut h, 16, nodes_start);
        put_u64(&mut h, 24, names_start);
        put_u64(&mut h, 32, names_start);
        put_u64(&mut h, 40, blocks_start);
        put_u64(&mut h, 48, blocks_start);
        put_u64(&mut h, 56, blobs_start);
        put_u64(&mut h, 64, blobs_start);
        put_u64(&mut h, 72, total);
        // root node is always id 0; root block from node 0
        h[80..84].copy_from_slice(&0u32.to_le_bytes());
        h[84..88].copy_from_slice(&self.first_block[0].to_le_bytes());
        // 88..128: flags + reserved, all zero
        h
    }

    fn serialize_nodes(&self, out: &mut Vec<u8>) {
        for (id, node) in self.nodes.iter().enumerate() {
            let mut rec = [0u8; NODE_SIZE as usize];
            rec[0] = node.kind.as_byte();
            let (name_off, name_len) = self.name_iv[id];
            rec[4..8].copy_from_slice(&name_off.to_le_bytes());
            rec[8..12].copy_from_slice(&name_len.to_le_bytes());
            let (data_off, data_len) = self.data_iv[id];
            rec[16..24].copy_from_slice(&data_off.to_le_bytes());
            rec[24..32].copy_from_slice(&data_len.to_le_bytes());
            rec[32..36].copy_from_slice(&self.first_block[id].to_le_bytes());
            rec[36..40].copy_from_slice(&(node.children.len() as u32).to_le_bytes());
            rec[40..48].copy_from_slice(&node.mode.to_le_bytes());
            rec[48..56].copy_from_slice(&node.mtime.to_le_bytes());
            out.extend_from_slice(&rec);
        }
    }

    fn serialize_blocks(&self, out: &mut Vec<u8>) {
        for block in &self.blocks {
            let mut buf = vec![0u8; BLOCK_SIZE as usize];
            buf[0] = block.kind;
            for (slot, (off, len, child)) in block.entries.iter().enumerate() {
                let base = 4 + slot * ENTRY_SIZE;
                buf[base..base + 4].copy_from_slice(&off.to_le_bytes());
                buf[base + 4..base + 8].copy_from_slice(&len.to_le_bytes());
                buf[base + 8..base + 16].copy_from_slice(&child.to_le_bytes());
            }
            for slot in block.entries.len()..FANOUT {
                let base = 4 + slot * ENTRY_SIZE;
                buf[base + 8..base + 16].copy_from_slice(&u64::MAX.to_le_bytes());
            }
            out.extend_from_slice(&buf);
        }
    }
}

fn default_mode(kind: NodeType) -> u64 {
    match kind {
        NodeType::Dir => 0o755,
        NodeType::File => 0o644,
        NodeType::Symlink => 0o777,
    }
}

fn put_u64(buf: &mut [u8], at: usize, v: u64) {
    buf[at..at + 8].copy_from_slice(&v.to_le_bytes());
}

fn field_str<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(|x| x.as_str())
        .ok_or_else(|| Error::Json(format!("missing or non-string field {key:?}")))
}

fn reject_children(v: &Value) -> Result<()> {
    if v.get("children").is_some() {
        return Err(Error::Json("file/symlink must not have children".into()));
    }
    Ok(())
}

fn validate_name(name: &str, is_root: bool) -> Result<()> {
    if is_root {
        if !name.is_empty() {
            return Err(Error::Json("root node must have empty name".into()));
        }
        return Ok(());
    }
    if name.is_empty() || name.contains('/') || name.contains('\0') {
        return Err(Error::Json(format!(
            "invalid name {name:?}: empty, or contains '/' or NUL"
        )));
    }
    if name == "." || name == ".." {
        return Err(Error::Json(format!("name {name:?} is reserved")));
    }
    Ok(())
}

/// Build an IFIX image from a parsed JSON request of the form
/// `{"tree": <node>, ...}`.
pub fn build_from_json(request: &Value, options: WriteOptions) -> Result<Vec<u8>> {
    let tree = request
        .get("tree")
        .ok_or_else(|| Error::Json("request missing \"tree\"".into()))?;
    let mut builder = Builder::new(options.limits);
    let root_id = builder.ingest(tree, true)?;
    if root_id != 0 {
        return Err(Error::Json("root must be the first node".into()));
    }
    if builder.nodes[0].kind != NodeType::Dir {
        return Err(Error::Json("root node must be a directory".into()));
    }
    builder.build_indexes()?;
    builder.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;
    use crate::reader::{Reader, SliceSource};

    fn build(req: &str) -> Vec<u8> {
        let v = parse(req).unwrap();
        build_from_json(&v, WriteOptions::default()).unwrap()
    }

    const TREE: &str = r#"{"tree":{"name":"","type":"dir","children":[
      {"name":"a.txt","type":"file","content_base64":"aGk="},
      {"name":"sub","type":"dir","children":[
        {"name":"deep","type":"symlink","target":"../a.txt"}
      ]},
      {"name":"z","type":"dir","children":[]}
    ]}}"#;

    #[test]
    fn build_and_lookup() {
        let img = build(TREE);
        let r = Reader::new(Box::new(SliceSource::new(img)), Limits::default()).unwrap();
        let f = r.lookup("/a.txt", true).unwrap();
        assert_eq!(f.kind, NodeType::File);
        assert_eq!(r.read_file(&f).unwrap(), b"hi");
        let s = r.lookup("/sub/deep", false).unwrap();
        assert_eq!(s.kind, NodeType::Symlink);
        let listing = r.list("/").unwrap();
        assert_eq!(
            listing.iter().map(|e| e.name.clone()).collect::<Vec<_>>(),
            vec!["a.txt", "sub", "z"]
        );
    }

    #[test]
    fn wide_tree_uses_multiple_levels() {
        let n = 400; // > FANOUT^2 leaves
        let mut children = String::new();
        for i in 0..n {
            children.push_str(&format!(
                "{{\"name\":\"f{i:05}\",\"type\":\"file\",\"content_base64\":\"\"}},"
            ));
        }
        children.pop(); // trailing comma
        let req = format!(r#"{{"tree":{{"name":"","type":"dir","children":[{children}]}}}}"#);
        let img = build(&req);
        let r = Reader::new(Box::new(SliceSource::new(img)), Limits::default()).unwrap();
        assert!(r.header().block_count > FANOUT as u32);
        let f = r.lookup("/f00399", true).unwrap();
        assert_eq!(f.kind, NodeType::File);
        assert!(r.lookup("/f00400", true).is_err());
        assert_eq!(r.list("/").unwrap().len(), n);
    }

    #[test]
    fn rejects_duplicate_and_bad_names() {
        let req = r#"{"tree":{"name":"","type":"dir","children":[
          {"name":"x","type":"dir","children":[]},
          {"name":"x","type":"dir","children":[]}]}}"#;
        let v = parse(req).unwrap();
        assert!(build_from_json(&v, WriteOptions::default()).is_err());
    }
}
