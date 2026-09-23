//! Embedded guest artifact: ELF (for execution/proving) and the expected
//! image ID (for verification). Generated at build time by `risc0-build`.

include!(concat!(env!("OUT_DIR"), "/methods.rs"));

/// Expected image ID for the batch-median guest, re-exported as a RISC Zero
/// [`Digest`] so host code can pass it straight to `Receipt::verify`.
pub fn image_id() -> risc0_zkvm::Digest {
    BATCH_MEDIAN_ID.into()
}
