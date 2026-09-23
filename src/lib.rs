//! Content-addressable block store.
//!
//! Crate layout:
//!
//! - [`hash`]  – SHA-256 helpers and hex validation (integrity checking only).
//! - [`vfs`]   – injectable I/O layer (`Vfs`), with a real filesystem backend,
//!              an in-memory backend and a fault-injecting backend used in tests.
//! - [`store`] – the [`store::Repository`]: block storage, named roots, atomic
//!              root publish and mark-sweep garbage collection.
//! - [`server`] – a small dependency-free HTTP validation frontend.

pub mod hash;
pub mod server;
pub mod store;
pub mod vfs;

pub use store::{
    BlockInfo, GcReport, MissingRef, Repository, RootEntry, StageGuard, StoreError, VerifyReport,
};
pub use vfs::{Fault, FaultFs, FaultOp, MemFs, RealFs, Vfs};
