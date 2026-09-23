//! tsblock: a file-backed integer time-series block store with delta-of-delta
//! timestamp encoding, first-difference value encoding, an injectable I/O
//! layer, and a minimal std-only HTTP verification surface.
//!
//! Module map:
//! * [`bitio`] — MSB-first bit buffer and bit-level unsigned varints
//! * [`coding`] — the block codec (timestamps, values, overflow escapes)
//! * [`crc`] — CRC-32/ISO-HDLC framing
//! * [`io_layer`] — `IoFile`/`IoBackend` traits, `RealIo` and `FaultyIo`
//! * [`store`] — on-disk format, block index, sync and ordering semantics
//! * [`server`] — the local HTTP verification endpoint

pub mod bitio;
pub mod coding;
pub mod crc;
pub mod io_layer;
pub mod server;
pub mod store;
