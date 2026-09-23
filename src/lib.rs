//! # Merkle State Proof Service
//!
//! A versioned key/value store backed by RocksDB whose every committed batch
//! produces an immutable SHA-256 Merkle root. Existence and non-existence can
//! be proven to — and independently verified by — any party holding only a
//! root hash.
//!
//! * [`hash`] — domain-separated leaf / branch / empty-tree encoding.
//! * [`tree`] — sorted-key tree construction and proof generation.
//! * [`proof`] — proof protocol types and the storage-independent verifier.
//! * [`store`] — RocksDB persistence and atomic version publishing.
//! * [`service`] / [`api`] — async service and Axum HTTP endpoints.

pub mod api;
pub mod encoding;
pub mod error;
pub mod hash;
pub mod proof;
pub mod service;
pub mod store;
pub mod tree;

pub use error::{Error, Result};
pub use proof::{
    verify_inclusion, verify_non_existence, verify_response, InclusionProof, NonExistenceProof,
    ProofResponse,
};
pub use service::Service;
pub use store::{Store, VersionInfo, WriteOp};
pub use tree::SnapshotTree;
