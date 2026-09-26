//! Error types shared by the writer, validator, and readers.

use std::fmt;
use std::io;

/// Result alias returned throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// All ways decoding or validating an IFIX file can fail.
///
/// Every variant that describes malformed input carries enough context to
/// identify the offending structure; none of them ever trigger allocation
/// proportional to a value read from the file.
#[derive(Debug)]
pub enum Error {
    /// Underlying I/O failure.
    Io(io::Error),
    /// File is shorter than the 128-byte superblock.
    TruncatedHeader,
    /// Magic bytes are not `IFIX\\n\\x00\\x01\\n`.
    BadMagic,
    /// Version byte is not supported (only `1` exists).
    UnsupportedVersion(u8),
    /// Reserved header bytes / flags are nonzero.
    ReservedSet {
        /// Which field.
        field: &'static str,
        /// Observed value.
        value: u64,
    },
    /// A declared section range is invalid (end before start, out of file...).
    BadSection {
        /// Section name.
        name: &'static str,
        /// Declared start offset.
        start: u64,
        /// Declared end offset.
        end: u64,
    },
    /// Sections are not in their mandated order or overlap.
    SectionOverlap {
        /// Earlier section name.
        earlier: &'static str,
        /// Later section name.
        later: &'static str,
    },
    /// The file ends before the declared end of a section.
    SectionTruncated {
        /// Section name.
        name: &'static str,
        /// Declared end offset.
        declared_end: u64,
        /// Actual file length.
        file_len: u64,
    },
    /// A section byte length is not a multiple of its record/block size.
    Misaligned {
        /// Section name.
        name: &'static str,
        /// Length in bytes.
        len: u64,
        /// Required alignment.
        alignment: u64,
    },
    /// A node index is outside the nodes section.
    BadNodeRef {
        /// Raw index found in the file.
        index: u64,
        /// Number of nodes actually present.
        count: u64,
    },
    /// A block index is outside the blocks section.
    BadBlockRef {
        /// Raw index found in the file.
        index: u64,
        /// Number of blocks actually present.
        count: u64,
    },
    /// A byte interval lies outside its owning section.
    OutOfRange {
        /// What the interval names (names / blob).
        what: &'static str,
        /// Start offset.
        start: u64,
        /// Length.
        len: u64,
        /// Upper bound it must stay within.
        bound: u64,
    },
    /// A name interval crosses another node's name interval (validator).
    NameOverlap {
        /// First node index.
        a: u32,
        /// Second node index.
        a_end: u64,
        /// Third node index.
        b: u32,
        /// Fourth node index.
        b_start: u64,
    },
    /// A blob interval crosses another file node's blob interval (validator).
    BlobOverlap {
        /// First node index.
        a: u32,
        /// Second node index.
        a_end: u64,
        /// Third node index.
        b: u32,
        /// Fourth node index.
        b_start: u64,
    },
    /// A child block index is not greater than its parent (acyclicity rule).
    NonForwardBlockRef {
        /// Parent block index.
        parent: u32,
        /// Child block index (must be > parent).
        child: u32,
    },
    /// Index navigation hit a null child where the tree promised one, or a
    /// child of the wrong kind/arity.
    CorruptIndex {
        /// Explanation.
        detail: &'static str,
    },
    /// A node-type byte is not one of the four valid values.
    BadNodeType(u8),
    /// A block's kind byte is not leaf or internal.
    BadBlockKind(u8),
    /// A leaf separator/child ordering invariant is violated.
    BadOrdering(&'static str),
    /// A node record failed a field-level check.
    BadNodeRecord {
        /// Node index.
        index: u32,
        /// What was wrong.
        detail: &'static str,
    },
    /// A short read occurred inside a section declared by the header (the
    /// file shrank or lied about its length).
    ShortRead {
        /// Absolute file offset.
        at: u64,
        /// Bytes expected.
        need: u64,
    },
    /// The root block index does not point at a valid block.
    BadRoot(u64),
    /// The requested path does not exist.
    NotFound(String),
    /// The requested path resolves to a node of the wrong kind.
    NotADirectory(String),
    /// A decoded name is not valid UTF-8.
    BadNameEncoding(u32),
    /// A decoded name is empty or contains a NUL or `/`.
    BadName(u32),
    /// A traversal exceeded a configured limit (node count, depth, output...).
    LimitExceeded {
        /// Which limit.
        what: &'static str,
        /// Configured bound.
        limit: u64,
    },
    /// A link chain contains a cycle or is longer than the depth limit.
    LinkCycleOrDepth(String),
    /// The JSON control document is malformed.
    Json(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "i/o error: {e}"),
            Error::TruncatedHeader => f.write_str("file shorter than 128-byte IFIX header"),
            Error::BadMagic => f.write_str("bad magic: not an IFIX file"),
            Error::UnsupportedVersion(v) => write!(f, "unsupported IFIX version {v}"),
            Error::ReservedSet { field, value } => {
                write!(f, "reserved field {field} is nonzero ({value})")
            }
            Error::BadSection { name, start, end } => {
                write!(f, "section {name}: invalid range [{start}, {end})")
            }
            Error::SectionOverlap { earlier, later } => {
                write!(
                    f,
                    "sections {later} starts before {earlier} ends (overlap/order)"
                )
            }
            Error::SectionTruncated {
                name,
                declared_end,
                file_len,
            } => write!(
                f,
                "section {name} declares end {declared_end} but file is {file_len} bytes"
            ),
            Error::Misaligned {
                name,
                len,
                alignment,
            } => {
                write!(
                    f,
                    "section {name} length {len} is not a multiple of {alignment}"
                )
            }
            Error::BadNodeRef { index, count } => {
                write!(
                    f,
                    "node reference {index} out of range (node_count={count})"
                )
            }
            Error::BadBlockRef { index, count } => {
                write!(
                    f,
                    "block reference {index} out of range (block_count={count})"
                )
            }
            Error::OutOfRange {
                what,
                start,
                len,
                bound,
            } => write!(
                f,
                "{what} interval [{start}, +{len}) exceeds section bound {bound}"
            ),
            Error::NameOverlap {
                a,
                a_end,
                b,
                b_start,
            } => write!(
                f,
                "name intervals of nodes {a} (ends {a_end}) and {b} (starts {b_start}) overlap"
            ),
            Error::BlobOverlap {
                a,
                a_end,
                b,
                b_start,
            } => write!(
                f,
                "blob intervals of nodes {a} (ends {a_end}) and {b} (starts {b_start}) overlap"
            ),
            Error::NonForwardBlockRef { parent, child } => write!(
                f,
                "block {parent} references child block {child} which is not greater (index cycle)"
            ),
            Error::CorruptIndex { detail } => write!(f, "corrupt offset index: {detail}"),
            Error::BadNodeType(t) => write!(f, "invalid node type byte {t:#04x}"),
            Error::BadBlockKind(k) => write!(f, "invalid index block kind {k:#04x}"),
            Error::BadOrdering(s) => write!(f, "index ordering violation: {s}"),
            Error::BadNodeRecord { index, detail } => {
                write!(f, "node {index}: invalid record: {detail}")
            }
            Error::ShortRead { at, need } => {
                write!(f, "short read at offset {at}: needed {need} bytes")
            }
            Error::BadRoot(r) => write!(f, "root_block {r} is not a valid block index"),
            Error::NotFound(p) => write!(f, "path not found: {p}"),
            Error::NotADirectory(p) => write!(f, "path is not a directory: {p}"),
            Error::BadNameEncoding(i) => write!(f, "node {i}: name is not valid UTF-8"),
            Error::BadName(i) => write!(f, "node {i}: empty name or name contains NUL or '/'"),
            Error::LimitExceeded { what, limit } => {
                write!(f, "limit exceeded: {what} > {limit}")
            }
            Error::LinkCycleOrDepth(p) => {
                write!(
                    f,
                    "symlink cycle or link-depth exceeded while resolving {p}"
                )
            }
            Error::Json(s) => write!(f, "invalid JSON request: {s}"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}
