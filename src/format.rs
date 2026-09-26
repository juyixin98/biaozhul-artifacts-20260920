//! On-disk constants, header parsing, and configured decode limits.
//!
//! All multi-byte integers are little-endian. The layout is fully specified in
//! `docs/FORMAT.md`; this module implements only the pieces that have no I/O
//! dependency so both readers can share them.

use crate::error::{Error, Result};

/// Magic prefix identifying an IFIX file: `IFIX\n\x00\x01\n`.
pub const MAGIC: [u8; 8] = [b'I', b'F', b'I', b'X', b'\n', 0x00, 0x01, b'\n'];

/// Total superblock size in bytes.
pub const HEADER_SIZE: u64 = 128;

/// Format version produced and accepted by this implementation.
pub const FORMAT_VERSION: u8 = 1;

/// Size of one serialized node record.
pub const NODE_SIZE: u64 = 64;

/// Size of one child entry inside an index block:
/// `u32 key_name_off + u32 key_name_len + u64 child`.
///
/// For internal blocks `child` is a block index (high 32 bits must be zero);
/// for leaf blocks it is a node id; the all-ones value (`u64::MAX`) marks an
/// inactive slot.
pub const ENTRY_SIZE: usize = 16;

/// Number of child entries per index block.
pub const FANOUT: usize = 16;

/// Size of an index block: 4-byte header + 16 * 16-byte entries = 260 bytes.
///
/// A deliberately odd, non-power-of-two size makes section-alignment bugs
/// immediately visible.
pub const BLOCK_SIZE: u64 = 4 + ENTRY_SIZE as u64 * FANOUT as u64; // 260

/// Node type: regular file.
pub const TYPE_FILE: u8 = 1;
/// Node type: directory.
pub const TYPE_DIR: u8 = 2;
/// Node type: symbolic link.
pub const TYPE_SYMLINK: u8 = 3;

/// Index block kind: leaf (children are node ids, separator = node id).
pub const KIND_LEAF: u8 = 1;
/// Index block kind: internal (children are block indices > self).
pub const KIND_INTERNAL: u8 = 2;

/// Sentinel meaning "absent" for ids, separators and child blocks.
pub const NIL: u32 = u32::MAX;

/// Node type with a Rust-facing name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeType {
    /// Regular file.
    File,
    /// Directory.
    Dir,
    /// Symbolic link (target stored in the blob section).
    Symlink,
}

impl NodeType {
    /// Convert the on-disk byte, rejecting anything outside 1..=3.
    pub fn from_byte(b: u8) -> Result<Self> {
        match b {
            TYPE_FILE => Ok(NodeType::File),
            TYPE_DIR => Ok(NodeType::Dir),
            TYPE_SYMLINK => Ok(NodeType::Symlink),
            _ => Err(Error::BadNodeType(b)),
        }
    }

    /// On-disk byte.
    pub fn as_byte(self) -> u8 {
        match self {
            NodeType::File => TYPE_FILE,
            NodeType::Dir => TYPE_DIR,
            NodeType::Symlink => TYPE_SYMLINK,
        }
    }
}

/// Parsed superblock. Offsets are absolute byte positions in the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    /// Absolute range of the node records section.
    pub nodes: (u64, u64),
    /// Absolute range of the packed name bytes section.
    pub names: (u64, u64),
    /// Absolute range of the index block section.
    pub blocks: (u64, u64),
    /// Absolute range of the blob section (file contents / symlink targets).
    pub blobs: (u64, u64),
    /// Root node index (must be a directory).
    pub root_node: u32,
    /// Root index block (must be a block, or NIL for an empty root).
    pub root_block: u32,
    /// Number of node records (derived, range-checked).
    pub node_count: u32,
    /// Number of index blocks (derived, range-checked).
    pub block_count: u32,
}

/// Configurable resource bounds for decoding.
///
/// Nothing in the decoder allocates based on a header value before the header
/// passes validation; these caps apply to *decoder output* (traversal, name
/// materialization) and to accepted file shapes.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// Maximum accepted file length in bytes.
    pub max_file_len: u64,
    /// Maximum number of node records.
    pub max_nodes: u64,
    /// Maximum number of index blocks.
    pub max_blocks: u64,
    /// Total bytes the client is willing to receive from one content read.
    pub max_output: u64,
    /// Maximum length of a single materialized name.
    pub max_name_len: u64,
    /// Maximum entries returned by one directory listing.
    pub max_listing: u64,
    /// Number of path components resolved per lookup (bounds symlink loops).
    pub max_depth: u32,
    /// Symlink chain length before giving up.
    pub max_link_depth: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_file_len: 1 << 30, // 1 GiB file cap
            max_nodes: 4_000_000,
            max_blocks: 4_000_000,
            max_output: 64 << 20, // 64 MiB decoded output cap
            max_name_len: 4096,
            max_listing: 1_000_000,
            max_depth: 128,
            max_link_depth: 40,
        }
    }
}

impl Limits {
    /// Parse/validate a raw 128-byte superblock under these limits.
    ///
    /// This never trusts the header: magic, version, reserved fields, section
    /// ordering/alignment, counts and the file length are all checked before
    /// any section is read. No allocation is performed.
    pub fn parse_header(buf: &[u8; HEADER_SIZE as usize], file_len: u64) -> Result<Header> {
        if buf[0..8] != MAGIC {
            return Err(Error::BadMagic);
        }
        let version = buf[8];
        if version != FORMAT_VERSION {
            return Err(Error::UnsupportedVersion(version));
        }
        // bytes 9..16 reserved
        for &b in &buf[9..16] {
            if b != 0 {
                return Err(Error::ReservedSet {
                    field: "header[9..16]",
                    value: b as u64,
                });
            }
        }

        let nodes = (read_u64(buf, 16), read_u64(buf, 24));
        let names = (read_u64(buf, 32), read_u64(buf, 40));
        let blocks = (read_u64(buf, 48), read_u64(buf, 56));
        let blobs = (read_u64(buf, 64), read_u64(buf, 72));
        let root_node = read_u32(buf, 80);
        let root_block = read_u32(buf, 84);
        let flags = read_u64(buf, 88);
        if flags != 0 {
            return Err(Error::ReservedSet {
                field: "flags",
                value: flags,
            });
        }
        // bytes 96..128 reserved
        for (i, &b) in buf[96..128].iter().enumerate() {
            if b != 0 {
                let _ = i;
                return Err(Error::ReservedSet {
                    field: "header[96..128]",
                    value: b as u64,
                });
            }
        }

        let sections: [(&str, (u64, u64), u64); 4] = [
            ("nodes", nodes, 0),
            ("names", names, 0),
            ("blocks", blocks, BLOCK_SIZE),
            ("blobs", blobs, 0),
        ];

        // Every range must be well-formed and start at/after the header.
        for (name, (s, e), _align) in sections {
            if s < HEADER_SIZE || e < s {
                return Err(Error::BadSection {
                    name,
                    start: s,
                    end: e,
                });
            }
            if e > file_len {
                return Err(Error::SectionTruncated {
                    name,
                    declared_end: e,
                    file_len,
                });
            }
        }

        // Strict ordering: sections are tightly packed in a fixed order with
        // no overlaps and no gaps, so every declared offset is cross-checked
        // against its neighbor rather than trusted on its own.
        let order: [(&str, (u64, u64)); 4] = [
            ("nodes", nodes),
            ("names", names),
            ("blocks", blocks),
            ("blobs", blobs),
        ];
        if order[0].1 .0 != HEADER_SIZE {
            return Err(Error::BadSection {
                name: "nodes",
                start: order[0].1 .0,
                end: order[0].1 .1,
            });
        }
        for w in order.windows(2) {
            let (earlier, (_, pe)) = w[0];
            let (later, (cs, ce)) = w[1];
            if cs != pe {
                return Err(Error::SectionOverlap { earlier, later });
            }
            debug_assert!(ce >= cs, "range validated above");
        }
        // File must end exactly at blobs_end (trailing bytes are rejected so a
        // declared length can never hide data past the validated sections).
        if blobs.1 != file_len {
            return Err(Error::SectionTruncated {
                name: "blobs",
                declared_end: blobs.1,
                file_len,
            });
        }

        // Alignment for fixed-record sections.
        let nodes_len = nodes.1 - nodes.0;
        if !nodes_len.is_multiple_of(NODE_SIZE) {
            return Err(Error::Misaligned {
                name: "nodes",
                len: nodes_len,
                alignment: NODE_SIZE,
            });
        }
        let blocks_len = blocks.1 - blocks.0;
        if !blocks_len.is_multiple_of(BLOCK_SIZE) {
            return Err(Error::Misaligned {
                name: "blocks",
                len: blocks_len,
                alignment: BLOCK_SIZE,
            });
        }

        let node_count = nodes_len / NODE_SIZE;
        let block_count = blocks_len / BLOCK_SIZE;
        if node_count > u32::MAX as u64 || block_count > u32::MAX as u64 {
            return Err(Error::BadSection {
                name: "count",
                start: node_count,
                end: block_count,
            });
        }

        // Root node must exist and root block must exist (or be the empty sentinel).
        if node_count == 0 {
            return Err(Error::BadSection {
                name: "nodes",
                start: nodes.0,
                end: nodes.1,
            });
        }
        if root_node as u64 >= node_count {
            return Err(Error::BadNodeRef {
                index: root_node as u64,
                count: node_count,
            });
        }
        if root_block != NIL && root_block as u64 >= block_count {
            return Err(Error::BadRoot(root_block as u64));
        }

        Ok(Header {
            nodes,
            names,
            blocks,
            blobs,
            root_node,
            root_block,
            node_count: node_count as u32,
            block_count: block_count as u32,
        })
    }

    /// Enforce the configurable caps on a parsed header.
    pub fn check_caps(&self, h: &Header) -> Result<()> {
        let file_len = h.blobs.1;
        if file_len > self.max_file_len {
            return Err(Error::LimitExceeded {
                what: "file_len",
                limit: self.max_file_len,
            });
        }
        if h.node_count as u64 > self.max_nodes {
            return Err(Error::LimitExceeded {
                what: "node_count",
                limit: self.max_nodes,
            });
        }
        if h.block_count as u64 > self.max_blocks {
            return Err(Error::LimitExceeded {
                what: "block_count",
                limit: self.max_blocks,
            });
        }
        Ok(())
    }
}

/// Read a little-endian u32 from the header (all offsets are in-bounds).
pub fn read_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([buf[at], buf[at + 1], buf[at + 2], buf[at + 3]])
}

/// Read a little-endian u64 from the header.
pub fn read_u64(buf: &[u8], at: usize) -> u64 {
    u64::from_le_bytes([
        buf[at],
        buf[at + 1],
        buf[at + 2],
        buf[at + 3],
        buf[at + 4],
        buf[at + 5],
        buf[at + 6],
        buf[at + 7],
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header_with(sections: [(u64, u64); 4], root_node: u32, root_block: u32) -> [u8; 128] {
        let mut b = [0u8; 128];
        b[0..8].copy_from_slice(&MAGIC);
        b[8] = FORMAT_VERSION;
        for (i, (s, e)) in sections.iter().enumerate() {
            b[16 + i * 16..16 + i * 16 + 8].copy_from_slice(&s.to_le_bytes());
            b[24 + i * 16..24 + i * 16 + 8].copy_from_slice(&e.to_le_bytes());
        }
        b[80..84].copy_from_slice(&root_node.to_le_bytes());
        b[84..88].copy_from_slice(&root_block.to_le_bytes());
        b
    }

    #[test]
    fn accepts_minimal_file() {
        // one node, no blocks, tightly packed: nodes 128..192, names 192..192,
        // blocks 192..192, blobs 192..192
        let h = header_with([(128, 192), (192, 192), (192, 192), (192, 192)], 0, NIL);
        let parsed = Limits::parse_header(&h, 192).unwrap();
        assert_eq!(parsed.node_count, 1);
        assert_eq!(parsed.block_count, 0);
    }

    #[test]
    fn rejects_bad_magic_and_version() {
        let mut h = header_with([(128, 192), (192, 192), (192, 192), (192, 192)], 0, NIL);
        h[0] = b'X';
        assert!(matches!(
            Limits::parse_header(&h, 192),
            Err(Error::BadMagic)
        ));
        let mut h = header_with([(128, 192), (192, 192), (192, 192), (192, 192)], 0, NIL);
        h[8] = 9;
        assert!(matches!(
            Limits::parse_header(&h, 192),
            Err(Error::UnsupportedVersion(9))
        ));
    }

    #[test]
    fn rejects_section_overlap_and_truncation() {
        // names start before nodes end while everything stays inside the file
        let h = header_with([(128, 200), (192, 200), (200, 200), (200, 256)], 0, NIL);
        assert!(matches!(
            Limits::parse_header(&h, 256),
            Err(Error::SectionOverlap { .. })
        ));
        // blobs end past EOF
        let h = header_with([(128, 192), (192, 192), (192, 192), (192, 300)], 0, NIL);
        assert!(matches!(
            Limits::parse_header(&h, 250),
            Err(Error::SectionTruncated { .. })
        ));
        // trailing bytes after blobs are rejected
        let h = header_with([(128, 192), (192, 192), (192, 192), (192, 192)], 0, NIL);
        assert!(matches!(
            Limits::parse_header(&h, 200),
            Err(Error::SectionTruncated { .. })
        ));
    }

    #[test]
    fn rejects_huge_declared_counts_without_allocating() {
        // Claim a gigantic node section that is also misaligned/truncated;
        // whichever fires first, it must be an error and must not allocate.
        let h = header_with([(128, u64::MAX), (0, 0), (0, 0), (0, 0)], 0, NIL);
        assert!(Limits::parse_header(&h, 192).is_err());
    }
}
