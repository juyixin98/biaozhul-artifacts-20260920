//! `cdict` — streaming columnar dictionary encoding for string columns.
//!
//! * Binary format: see `docs/FORMAT.md` (magic `CDCT`, version 1).
//! * Core algorithms (dictionary interning, segmented merge with
//!   cross-segment id remapping, LEB128 varints, JSON control plane) are
//!   implemented in this crate with no third-party dependencies.
//! * Memory and output bounds are enforced through [`Limits`].
//!
//! Quick start:
//!
//! ```
//! use cdict::{encode_to_vec, decode_to_vec, Limits};
//!
//! let rows = vec![Some("apple"), None, Some("banana"), Some("apple")];
//! let bytes = encode_to_vec(&rows, &Limits::default(), 0).unwrap();
//! let (decoded, stats) = decode_to_vec(&bytes[..], &Limits::default()).unwrap();
//! assert_eq!(stats.rows, 4);
//! assert_eq!(decoded, rows.iter().map(|r| r.map(str::to_string)).collect::<Vec<_>>());
//! ```

pub mod control;
pub mod decoder;
pub mod encoder;
pub mod error;
pub mod json;
pub mod merge;
pub mod segment;
pub mod varint;

pub use decoder::{decode_for_each, decode_to_vec, DecodeStats, Decoder};
pub use encoder::{encode_to_vec, EncodeStats, Encoder, SegmentEncoder};
pub use error::{Error, Limits, Result};
pub use merge::{merge_to_vec, normalize_to_vec, MergeStats, Merger};
