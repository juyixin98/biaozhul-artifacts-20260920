//! OCI layered whitelist unpack — library crate.
//!
//! Pure-backend offline OCI image rootfs rebuilder: verifies layer digests,
//! applies layers in order with whiteout/opaque-dir semantics, enforces a
//! strict entry-type whitelist and path/link confinement, and publishes the
//! resulting rootfs only if every check succeeded.

pub mod digest;
pub mod error;
pub mod extractor;
pub mod limits;
pub mod oci;
pub mod pathsafe;
pub mod rebuild;
pub mod server;

pub use digest::OciDigest;
pub use error::{AppError, AppResult};
pub use limits::Limits;
pub use rebuild::{RebuildReport, Workdir};
