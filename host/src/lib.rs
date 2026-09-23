//! Batch-median verifiable-computation host library.

pub mod io_types;
pub mod service;
pub mod verifier;

pub use io_types::{
    journal_summary, BatchFile, CycleStats, ReceiptEnvelope, ResultReport, Timings,
};
pub use service::{
    admit, execute, execute_unchecked, image_id_hex, prove, prove_unchecked, ExecutionResult,
    ProveResult,
};
pub use verifier::{flavor_of, verify_and_admit, ReceiptFlavor, VerifiedOutput};
