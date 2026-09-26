//! On-disk format constants and decode/encode safety limits.
//!
//! See `docs/FORMAT.md` for the normative description.

/// Magic byte string at the start of every IFIX file: `IFIX1\0` (6 bytes).
pub const MAGIC: [u8; 6] = *b"IFIX1\0";
/// Supported format version.
pub const VERSION: u16 = 1;
/// Header length in bytes.
pub const HEADER_LEN: u64 = 48;
/// Size of one table-of-contents entry (`offset:u64`, `length:u32`).
pub const TOC_ENTRY_LEN: u64 = 13;
/// Root node id.
pub const ROOT_ID: u32 = 0;
/// Sentinel meaning "no children" in a node's `first_child` field.
pub const NONE_ID: u32 = 0xFFFF_FFFF;
/// Maximum name length encodable by the `name_len:u8` field.
pub const MAX_NAME_HARD: usize = u8::MAX as usize; // 255
/// Maximum children per node encodable by the `child_count:u16` field.
pub const MAX_CHILDREN_HARD: usize = u16::MAX as usize; // 65535

/// Safety limits applied while decoding untrusted files or streaming input.
///
/// Every field declared by a file is cross-checked against these caps *before*
/// any allocation proportional to it is made.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Whole file may not exceed this size.
    pub max_file_bytes: u64,
    /// Maximum number of nodes.
    pub max_nodes: u32,
    /// Maximum size of the data region.
    pub max_data_bytes: u64,
    /// Maximum TOC size.
    pub max_toc_bytes: u64,
    /// Maximum individual name length (<= 255 due to the wire format).
    pub max_name_bytes: usize,
    /// Maximum path depth resolved by lookup / traversal.
    pub max_depth: u32,
    /// Maximum bytes a single decoding command may collect into one response.
    pub max_output_bytes: u64,
    /// Maximum bytes accepted from a JSON input stream while building.
    pub max_input_bytes: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_file_bytes: 256 * 1024 * 1024,
            max_nodes: 1_000_000,
            max_data_bytes: 128 * 1024 * 1024,
            max_toc_bytes: 16 * 1024 * 1024,
            max_name_bytes: 255,
            max_depth: 1024,
            max_output_bytes: 16 * 1024 * 104, // 16 MiB
            max_input_bytes: 512 * 1024 * 1024,
        }
    }
}

impl Limits {
    pub fn check_name_len(&self, len: usize) -> crate::Result<()> {
        if len > MAX_NAME_HARD {
            return Err(crate::IfixError::format(
                "NAME_LEN",
                format!("name length {len} exceeds format maximum {MAX_NAME_HARD}"),
            ));
        }
        if len > self.max_name_bytes {
            return Err(crate::IfixError::format(
                "LIMIT_NAME",
                format!(
                    "name length {len} exceeds configured limit {}",
                    self.max_name_bytes
                ),
            ));
        }
        Ok(())
    }
}

// --- little-endian fixed-width helpers -------------------------------------

pub fn u16le(b: &[u8]) -> u16 {
    u16::from_le_bytes([b[0], b[1]])
}

pub fn u32le(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

pub fn u64le(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

/// Checked `offset + len <= bound`.
pub fn range_within(offset: u64, len: u64, bound: u64) -> bool {
    match offset.checked_add(len) {
        Some(end) => end <= bound,
        None => false,
    }
}
