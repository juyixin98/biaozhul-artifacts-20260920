//! # brpc — binary RPC multiplexing (pure backend, std-only)
//!
//! A hand-written incremental byte parser, a versioned length+CRC frame
//! protocol, and a multiplexed client/server pair over plain TCP.
//!
//! * [`crc32`] — table-driven CRC-32/IEEE implemented from scratch.
//! * [`frame`] — wire format, [`frame::Frame`] encode + header subset parse.
//! * [`decode`] — the incremental parser: half packets, sticky packets,
//!   payload length caps, explicit [`decode::DecodeError`]s, poison-on-fatal.
//! * [`payload`] — tiny demo RPC application layer (methods, result codes).
//! * [`client`] — [`client::MuxClient`]: one connection, many concurrent
//!   requests, non-reused slot+generation request ids, cancel, late-response
//!   detection, bounded in-flight memory.
//! * [`server`] — [`server::Server`]: demux reader, worker threads,
//!   cooperative cancellation, framing-error connection handling.

pub mod client;
pub mod crc32;
pub mod decode;
pub mod frame;
pub mod payload;
pub mod server;

pub use decode::{DecodeError, FrameDecoder};
pub use frame::{Frame, FrameKind, HEADER_LEN, MAGIC, VERSION};
