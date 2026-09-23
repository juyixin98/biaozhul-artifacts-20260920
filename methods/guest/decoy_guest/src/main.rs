//! A *different* zkVM program with a different image ID.
//!
//! It does honest but unrelated work (halves the values and commits the
//! count). A receipt from this guest proves `DECOY_GUEST_ID` executed, not
//! `MEDIAN_GUEST_ID`; verification against the expected median image ID must
//! fail with a claim/image-ID mismatch.

use risc0_zkvm::{
    guest::env,
    sha::{Impl, Sha256},
};
use shared::{encode_batch, JournalData};

fn main() {
    let values: Vec<i32> = env::read();

    // Note: deliberately no range validation and a different "result" — this
    // is a distinct program, not a variant of the median guest.
    let digest = Impl::hash_bytes(&encode_batch(&values));
    let mut input_commitment = [0u8; 32];
    input_commitment.copy_from_slice(digest.as_bytes());

    // Bogus median: sum-based value, clearly not the median.
    let fake_median: i32 = values.iter().map(|v| v / 2).sum::<i32>();

    env::commit(&JournalData {
        input_commitment,
        length: values.len() as u32,
        median: fake_median,
    });
}
