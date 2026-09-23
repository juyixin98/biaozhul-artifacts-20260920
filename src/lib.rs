//! Bounded-memory, file-backed stable external sort.
//!
//! See `README.md` for the architecture and disk format. Module map:
//!
//! * [`error`] — crate error type
//! * [`crc`] — CRC-32 used for segment integrity
//! * [`io`] — injectable VFS / file layer with fault injection
//! * [`key`] — composite key extraction and asc/desc comparison
//! * [`budget`] — memory accounting (buffers included)
//! * [`sortfile`] — on-disk run segment format and streaming codec
//! * [`scanner`] — bounded-memory line scanner with giant-record streaming
//! * [`repository`] — job directory, manifest, status and crash recovery
//! * [`sort`] — map + multi-pass k-way merge engine
//! * [`server`] — local HTTP validation endpoint
//! * [`reference`] — in-memory reference sort used by the acceptance tests

pub mod budget;
pub mod crc;
pub mod error;
pub mod io;
pub mod key;
pub mod reference;
pub mod repository;
pub mod scanner;
pub mod server;
pub mod sort;
pub mod sortfile;
pub mod testutil;

pub use error::{Error, Result};
pub use repository::{Job, JobConfig, JobState, Repository, RunEntry};
pub use sort::{run_job, SortStats};
