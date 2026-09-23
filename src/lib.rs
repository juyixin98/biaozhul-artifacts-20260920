//! tsblock — time-series block encoding, pure backend.
//!
//! Modules:
//! - [`codec`]: block encoding (delta-of-delta timestamps, delta values,
//!   zigzag varints, explicit escape for i64-overflowing deltas).
//! - [`io`]: injectable file I/O layer (`FileIO` trait, `FsIO`, `MockIO`).
//! - [`storage`]: file-backed repository: block index, range reads,
//!   out-of-order handling, crash recovery.
//! - [`http`]: minimal local HTTP validation endpoint.
//! - [`json`]: minimal JSON parser (just enough for the ingest API).

pub mod codec;
pub mod http;
pub mod io;
pub mod json;
pub mod storage;
