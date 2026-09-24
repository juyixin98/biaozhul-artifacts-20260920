//! OCI layered whitelist unpacker — library surface.
//!
//! Pure backend for rebuilding local, offline OCI image fixtures into a
//! whitelisted rootfs, layer by layer, applying OCI whiteouts and opaque
//! directories while rejecting path traversal, escaping symlinks, device
//! files and decompression bombs. Layer digests are verified (real SHA-256)
//! before a layer participates in the rebuild, and a failed rebuild never
//! publishes a partial rootfs.

pub mod digest;
pub mod error;
pub mod fsops;
pub mod io_util;
pub mod layer;
pub mod limits;
pub mod model;
pub mod path;
pub mod rebuild;
pub mod server;

pub use error::{Error, Result};
pub use limits::Limits;
pub use model::Store;
pub use rebuild::{rebuild, Builds, RebuildResult};
