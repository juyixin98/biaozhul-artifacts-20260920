//! Protocol primitives shared by the zkVM guest and the host.
//!
//! Everything that defines "what is being claimed" lives here, so that guest
//! and host can never disagree on the wire format:
//!
//! * [`MAX_BATCH_LEN`] / [`MIN_VALUE`] / [`MAX_VALUE`] — the validation rules
//!   enforced inside the guest.
//! * [`canonical_input_bytes`] — the exact byte string whose SHA-256 digest is
//!   the public input commitment. Host and guest hash the same bytes, so the
//!   host can independently re-compute the commitment from the raw inputs.
//! * [`Journal`] — the public, receipt-bound statement: magic/version, input
//!   commitment, batch length and the computed median.
//!
//! The crate is `no_std` capable (it builds for the `riscv32im-risc0-zkvm-elf`
//! target) and pulls in `alloc` only.

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

use alloc::vec::Vec;
use risc0_zkvm::sha::{Impl, Sha256};
use risc0_zkvm::Digest;
use serde::{Deserialize, Serialize};

/// Maximum number of signed integers in one batch.
pub const MAX_BATCH_LEN: usize = 256;

/// Inclusive lower bound for every batch element.
pub const MIN_VALUE: i64 = -1_000_000;

/// Inclusive upper bound for every batch element.
pub const MAX_VALUE: i64 = 1_000_000;

/// Magic prefix identifying a batch-median journal (`BMDJ` = "Batch MeDian Journal").
pub const JOURNAL_MAGIC: [u8; 4] = [0x42, 0x4d, 0x44, 0x4a];

/// Journal schema version.
pub const JOURNAL_VERSION: u16 = 1;

/// Errors that can occur when validating inputs on either side of the zkVM boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum BatchError {
    /// The batch is empty; a median of nothing is undefined.
    Empty,
    /// The batch has more than [`MAX_BATCH_LEN`] elements.
    TooLong { len: usize },
    /// An element is outside [`MIN_VALUE`, `MAX_VALUE`].
    OutOfRange { index: usize, value: i64 },
}

impl BatchError {
    pub fn as_str(self) -> &'static str {
        match self {
            BatchError::Empty => "empty batch: median is undefined",
            BatchError::TooLong { .. } => "batch exceeds the 256-element maximum",
            BatchError::OutOfRange { .. } => {
                "element outside the allowed range [-1000000, 1000000]"
            }
        }
    }
}

/// Validate batch length and per-element range. This is the exact predicate
/// the guest enforces; the host uses it to fail fast *before* proving.
pub fn validate(values: &[i64]) -> Result<(), BatchError> {
    if values.is_empty() {
        return Err(BatchError::Empty);
    }
    if values.len() > MAX_BATCH_LEN {
        return Err(BatchError::TooLong { len: values.len() });
    }
    for (index, &value) in values.iter().enumerate() {
        if !(MIN_VALUE..=MAX_VALUE).contains(&value) {
            return Err(BatchError::OutOfRange { index, value });
        }
    }
    Ok(())
}

/// Compute the median of a batch **without mutating the caller's slice**:
/// a copy is sorted, so the original input order stays available for
/// commitment / logging.
///
/// - Odd length: the middle element.
/// - Even length: the lower of the two middle elements. Values are bounded
///   integers, so this is a well-defined, reproducible tie-break (documented in
///   the README).
pub fn median_of(values: &[i64]) -> i64 {
    let mut sorted: Vec<i64> = values.to_vec();
    sorted.sort_unstable();
    sorted[(sorted.len() - 1) / 2]
}

/// Canonical serialization of a batch for commitment.
///
/// Layout: `u64` little-endian length, followed by each `i64` in little
/// endian, in the original (pre-sort) order. Length-prefix + fixed-width
/// fields make the encoding injective: two different batches never map to the
/// same byte string.
pub fn canonical_input_bytes(values: &[i64]) -> Vec<u8> {
    let mut out = Vec::with_capacity(8 + 8 * values.len());
    out.extend_from_slice(&(values.len() as u64).to_le_bytes());
    for v in values {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

/// SHA-256 input commitment. Uses `risc0_zkvm::sha::Impl`, which selects the
/// accelerated, circuit-native implementation on the guest and the standard
/// implementation on the host, so both sides compute identical digests.
pub fn input_commitment(values: &[i64]) -> Digest {
    *Impl::hash_bytes(&canonical_input_bytes(values))
}

/// The public statement committed by the receipt journal.
///
/// Fields are all fixed width so the encoding is unambiguous:
///
/// | offset | size | field |
/// |--------|------|-------|
/// | 0      | 4    | magic `BMDJ` |
/// | 4      | 2    | little-endian [`JOURNAL_VERSION`] |
/// | 6      | 2    | reserved (zero) |
/// | 8      | 8    | little-endian batch length `n` |
/// | 16     | 8    | little-endian median (`i64`) |
/// | 24     | 32   | SHA-256 commitment over the canonical input encoding |
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Journal {
    /// Number of elements in the proven batch (1..=256).
    pub length: u64,
    /// Median of the sorted batch.
    pub median: i64,
    /// SHA-256 commitment to the canonical input encoding.
    pub input_commitment: Digest,
}

impl Journal {
    /// Total serialized length in bytes.
    pub const ENCODED_SIZE: usize = 24 + 32;

    /// Build a journal from concrete inputs, computing the commitment host/guest
    /// agnostically. Does **not** validate; callers running untrusted data
    /// through it should [`validate`] first (the guest always does).
    pub fn new(values: &[i64], median: i64) -> Self {
        Self {
            length: values.len() as u64,
            median,
            input_commitment: input_commitment(values),
        }
    }

    /// Fixed, dependency-free byte encoding used as the zkVM journal. We use
    /// this instead of a serde format so that tampering tests and byte-level
    /// reasoning stay simple.
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(Self::ENCODED_SIZE);
        out.extend_from_slice(&JOURNAL_MAGIC);
        out.extend_from_slice(&JOURNAL_VERSION.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // reserved
        out.extend_from_slice(&self.length.to_le_bytes());
        out.extend_from_slice(&self.median.to_le_bytes());
        out.extend_from_slice(self.input_commitment.as_bytes());
        debug_assert_eq!(out.len(), Self::ENCODED_SIZE);
        out
    }

    /// Parse and structurally validate a journal.
    ///
    /// This only checks *shape and self-consistency* (magic, version, length
    /// bounds). Cryptographic authentication — that these bytes are actually
    /// covered by the receipt seal — is provided separately by
    /// `Receipt::verify(expected_image_id)` on the host.
    pub fn decode(bytes: &[u8]) -> Result<Self, JournalError> {
        if bytes.len() != Self::ENCODED_SIZE {
            return Err(JournalError::BadLength { got: bytes.len() });
        }
        if bytes[0..4] != JOURNAL_MAGIC {
            return Err(JournalError::BadMagic);
        }
        let version = u16::from_le_bytes([bytes[4], bytes[5]]);
        if version != JOURNAL_VERSION {
            return Err(JournalError::BadVersion { got: version });
        }
        let length = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
        if length == 0 || length as usize > MAX_BATCH_LEN {
            return Err(JournalError::BadLengthField { got: length });
        }
        let median = i64::from_le_bytes(bytes[16..24].try_into().unwrap());
        let mut digest_bytes = [0u8; 32];
        digest_bytes.copy_from_slice(&bytes[24..56]);
        Ok(Self {
            length,
            median,
            input_commitment: digest_bytes.into(),
        })
    }
}

/// Shape-level journal validation failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JournalError {
    /// Journal byte length is not [`Journal::ENCODED_SIZE`].
    BadLength { got: usize },
    /// Magic prefix mismatch.
    BadMagic,
    /// Unsupported schema version.
    BadVersion { got: u16 },
    /// Length field outside 1..=256.
    BadLengthField { got: u64 },
}

impl JournalError {
    pub fn as_str(self) -> &'static str {
        match self {
            JournalError::BadLength { .. } => "journal has an invalid byte length",
            JournalError::BadMagic => "journal magic prefix mismatch",
            JournalError::BadVersion { .. } => "unsupported journal schema version",
            JournalError::BadLengthField { .. } => {
                "journal length field outside the allowed range 1..=256"
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn medians_are_documented_tie_break() {
        assert_eq!(median_of(&[5]), 5);
        assert_eq!(median_of(&[3, 1, 2]), 2);
        // even: lower of the two middles
        assert_eq!(median_of(&[10, 20, 30, 40]), 20);
        assert_eq!(median_of(&[-5, -1, 2, 7]), -1);
    }

    #[test]
    fn validation_rules() {
        assert_eq!(validate(&[]), Err(BatchError::Empty));
        assert_eq!(
            validate(&vec![0i64; 257]),
            Err(BatchError::TooLong { len: 257 })
        );
        assert_eq!(
            validate(&[1, 1_000_001]),
            Err(BatchError::OutOfRange {
                index: 1,
                value: 1_000_001
            })
        );
        assert_eq!(
            validate(&[-1_000_001]),
            Err(BatchError::OutOfRange {
                index: 0,
                value: -1_000_001
            })
        );
        assert!(validate(&[MIN_VALUE, MAX_VALUE]).is_ok());
    }

    #[test]
    fn journal_roundtrip_and_binding() {
        let values = [9i64, -1, 1_000_000, 0, 42];
        let med = median_of(&values);
        let j = Journal::new(&values, med);
        let bytes = j.encode();
        assert_eq!(bytes.len(), Journal::ENCODED_SIZE);
        assert_eq!(Journal::decode(&bytes).unwrap(), j);

        // commitment binds order and values
        let reordered = [42i64, -1, 1_000_000, 0, 9];
        assert_ne!(input_commitment(&values), input_commitment(&reordered));
    }
}
