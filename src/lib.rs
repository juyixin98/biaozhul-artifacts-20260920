//! mvcc-reclaim: file-backed single-machine MVCC key/value store with
//! snapshot transactions, first-committer-wins write/write conflict detection
//! and snapshot-aware version reclamation.
//!
//! See the repository README for the disk format and acceptance walkthrough.

pub mod crc;
pub mod http;
pub mod io;
pub mod json;
pub mod mvcc;
pub mod store;

pub use mvcc::{CommitError, Engine, ReadTxn, Stats, WriteTxn};
pub use store::{GcReport, Mutation};
