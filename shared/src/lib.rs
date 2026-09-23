//! Types and logic shared between the zkVM guest and the host.
//!
//! Everything here is `no_std` + `alloc` so the exact same code runs inside the
//! rv32im guest and on the host. In particular, the range check, the median
//! rule and the *canonical encoding* used for the input commitment are
//! defined once, guaranteeing the guest's commitment and the host's
//! re-computation can never drift apart.

#![cfg_attr(target_os = "zkvm", no_std)]

extern crate alloc;

use alloc::vec::Vec;
use serde::{Deserialize, Serialize};

/// Maximum number of signed integers allowed in one batch.
pub const MAX_BATCH_LEN: usize = 256;

/// Inclusive range every input element must lie in. Chosen so a batch of
/// values fits comfortably inside the guest cycle budget while still being a
/// *non-trivial* range (the whole `i32` domain would make the check vacuous).
pub const MIN_VALUE: i32 = -10_000;
pub const MAX_VALUE: i32 = 10_000;

/// A value lies outside the committed range [`MIN_VALUE`]..=[`MAX_VALUE`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutOfRangeError {
    /// Index of the offending element.
    pub index: usize,
    /// The offending value.
    pub value: i32,
}

/// Encode a batch into its canonical, platform-independent byte representation.
///
/// Layout: little-endian `u32` length followed by each `i32` in little
/// endian. The input commitment in the journal is `SHA-256` of exactly these
/// bytes. Both the guest (`risc0_zkvm::sha::Impl`) and the host (RustCrypto
/// `sha2`) hash the identical encoding, so a host-supplied batch binds to the
/// receipt iff its commitment matches.
pub fn encode_batch(values: &[i32]) -> Vec<u8> {
    // 4-byte length prefix + 4 bytes per element.
    let mut out = Vec::with_capacity(4 + values.len() * 4);
    out.extend_from_slice(&(values.len() as u32).to_le_bytes());
    for v in values {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

/// Validate batch rules enforced by the guest:
///
/// * non-empty,
/// * at most [`MAX_BATCH_LEN`] elements,
/// * every element within [`MIN_VALUE`]..=[`MAX_VALUE`].
pub fn validate_batch(values: &[i32]) -> Result<(), BatchError> {
    if values.is_empty() {
        return Err(BatchError::Empty);
    }
    if values.len() > MAX_BATCH_LEN {
        return Err(BatchError::TooLong(values.len()));
    }
    for (index, &value) in values.iter().enumerate() {
        if !(MIN_VALUE..=MAX_VALUE).contains(&value) {
            return Err(BatchError::OutOfRange(OutOfRangeError { index, value }));
        }
    }
    Ok(())
}

/// Ways a batch can fail validation. Mirrors the guest's rejection reasons.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BatchError {
    /// The batch contained no elements.
    Empty,
    /// The batch had more than [`MAX_BATCH_LEN`] elements (carries the length).
    TooLong(usize),
    /// An element was outside [`MIN_VALUE`]..=[`MAX_VALUE`].
    OutOfRange(OutOfRangeError),
}

/// Compute the median after sorting, using the **lower median** for
/// even-length batches (the element at index `(n-1)/2` after sorting).
///
/// The lower-median rule is used instead of averaging the two middle values
/// because the result must remain an integer that is itself in range.
///
/// Returns `None` for an empty slice.
pub fn sorted_median(values: &[i32]) -> Option<i32> {
    if values.is_empty() {
        return None;
    }
    let mut sorted: Vec<i32> = values.to_vec();
    sorted.sort_unstable();
    // Lower median: index (len-1)/2 works for both odd and even lengths.
    Some(sorted[(values.len() - 1) / 2])
}

/// Public journal committed by the guest.
///
/// It binds three things, exactly as required:
/// * `input_commitment` — SHA-256 over the canonical encoding of the input
///   batch (32 bytes),
/// * `length` — number of elements,
/// * `median` — the verified result.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct JournalData {
    /// SHA-256 digest of [`encode_batch`] applied to the guest's input.
    pub input_commitment: [u8; 32],
    /// Number of elements in the batch (`1..=256`).
    pub length: u32,
    /// Sorted lower-median of the batch.
    pub median: i32,
}
