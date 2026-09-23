//! Content-addressed object store with reference counting and concurrency-safe
//! garbage collection. See module docs for the design.

pub mod http;
pub mod store;

pub use http::router;
pub use store::{
    hash_bytes, parse_manifest, validate_hash, Error, GcReport, ObjectKind, Store,
    UploadSummary,
};
