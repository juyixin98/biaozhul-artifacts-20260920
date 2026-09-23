//! PSST — a file-backed, prefix-compressed, strictly ordered table.
//!
//! Crate layout:
//!
//! * [`error`] — crate-wide error and result types.
//! * [`coding`] — varint / fixed-width little-endian codecs, CRC32C (Castagnoli)
//!   with the LevelDB-style checksum mask, and hex helpers.
//! * [`format`] — on-disk constants, [`format::BlockHandle`] and [`format::Footer`].
//! * [`io`] — injectable I/O traits (`WritableFile`, `RandomAccessFile`), a POSIX
//!   implementation, an in-memory implementation and fault injectors.
//! * [`block`] — read-only data/index blocks with in-block prefix compression,
//!   restart points and strict structural validation.
//! * [`table`] — the table builder (write path) and [`table::Table`] (read path):
//!   point gets, range scans, full validation.
//! * [`server`] — a dependency-free local HTTP verification endpoint.

pub mod block;
pub mod coding;
pub mod error;
pub mod format;
pub mod io;
pub mod server;
pub mod table;

pub use error::{Error, Result};
