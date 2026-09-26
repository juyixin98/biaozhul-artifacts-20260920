//! IFIX: immutable file index format.
//!
//! A pure-std, dependency-free streaming binary codec for a read-only tree of
//! file/directory/symlink nodes, backed by a multi-level (B+-tree style) offset
//! index that allows single-path lookups without scanning the file.
//!
//! See `docs/FORMAT.md` for the on-disk specification.

pub mod base64;
pub mod error;
pub mod format;
pub mod json;
pub mod mmap;
pub mod reader;
pub mod source;
pub mod validate;
pub mod writer;

pub use error::{Error, Result};
pub use format::{Header, Limits, NodeType, BLOCK_SIZE, FANOUT, HEADER_SIZE, NODE_SIZE};
pub use mmap::Mmap;
pub use reader::{DirEntry, NodeInfo, RawEntry, Reader};
pub use source::{FileSource, MmapSource, SliceSource, Source};
pub use validate::ValidationReport;
pub use writer::{build_from_json, WriteOptions};
