//! zkVM guest: range-check a batch of at most 256 signed integers and compute
//! the median of the sorted batch.
//!
//! Trust boundary
//! -------------
//! Inputs arrive from the host over the zkVM input stream and are treated as
//! **untrusted**. The guest enforces every rule itself:
//!
//! 1. length in `1..=MAX_BATCH_LEN` (256),
//! 2. every element in `[MIN_VALUE, MAX_VALUE]`,
//! 3. the median is computed over an in-guest sorted copy.
//!
//! Only after all checks pass does the guest commit a [`Journal`] (magic,
//! version, length, median, SHA-256 input commitment) to the public journal.
//! Any failure aborts with a non-zero exit code, which the prover refuses to
//! prove (`prove_guest_errors = false`) and the host reports honestly.

#![no_main]

use batch_median_core::{median_of, validate, Journal, MAX_BATCH_LEN};
use risc0_zkvm::guest::env;

risc0_zkvm::entry!(main);

fn main() {
    // Length-prefixed input: host writes one u32 then that many i64 values.
    let len: u32 = env::read();

    // Defend against an absurd allocation before reading the body.
    if len as usize > MAX_BATCH_LEN {
        panic!("batch length {len} exceeds maximum {MAX_BATCH_LEN}",);
    }

    let mut values = vec![0i64; len as usize];
    env::read_slice(&mut values);

    // Range + emptiness checks. A panic here faults the guest (non-zero exit);
    // no journal is produced and no receipt can claim success.
    if let Err(err) = validate(&values) {
        panic!("invalid batch: {}", err.as_str());
    }

    // Sorted copy: original order is preserved for the commitment.
    let median = median_of(&values);

    let journal = Journal::new(&values, median);
    let bytes = journal.encode();

    // Defensive: a malformed journal must never be committed.
    Journal::decode(&bytes).expect("journal encoding must round-trip");

    env::commit_slice(&bytes);
}
