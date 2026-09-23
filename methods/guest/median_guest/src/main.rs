//! zkVM guest: range-checked batch median with a public commitment.
//!
//! Reads a `Vec<i32>` from the host, enforces the batch rules
//! (`shared::validate_batch`) *inside the zkVM*, sorts and computes the lower
//! median, and commits a [`shared::JournalData`] binding the input commitment,
//! length and result.
//!
//! A violation panics inside the guest: execution faults, and — with
//! `prove_guest_errors = false` — no receipt is produced. The host therefore
//! cannot obtain a verified result for an out-of-range or empty batch.

use risc0_zkvm::{
    guest::env,
    sha::{Impl, Sha256},
};
use shared::{encode_batch, sorted_median, validate_batch, JournalData};

fn main() {
    // Input is supplied privately by the host; only its commitment is public.
    let values: Vec<i32> = env::read();

    // Enforce every rule inside the zkVM. This is the range check:
    // 1..=256 elements, each in -10_000..=10_000. A failure halts the guest
    // with a non-zero exit code and prevents a successful receipt.
    validate_batch(&values).unwrap_or_else(|err| {
        panic!("guest rejected batch: {err:?}");
    });

    // Commitment is computed in-guest over the canonical encoding.
    let digest = Impl::hash_bytes(&encode_batch(&values));
    let mut input_commitment = [0u8; 32];
    input_commitment.copy_from_slice(digest.as_bytes());

    let median = sorted_median(&values)
        .expect("non-empty batch guaranteed by validate_batch") as i32;

    let journal = JournalData {
        input_commitment,
        length: values.len() as u32,
        median,
    };

    // Public output, authenticated by the receipt seal.
    env::commit(&journal);
}
