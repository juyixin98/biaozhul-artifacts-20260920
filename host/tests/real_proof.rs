//! End-to-end tests with REAL cryptographic proving (composite STARK receipts
//! via the local `r0vm`). These are `#[ignore]`d because CPU proving takes far
//! longer than dev mode. Run them explicitly:
//!
//! ```sh
//! cargo test --release --test real_proof -- --ignored --nocapture
//! ```
//!
//! Failures are asserted as errors and reported honestly: a bad batch never
//! yields a receipt, and a bad receipt never verifies.

use methods::{DECOY_GUEST_ELF, DECOY_GUEST_ID, MEDIAN_GUEST_ELF, MEDIAN_GUEST_ID};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, InnerReceipt, Prover, ProverOpts};
use shared::JournalData;
use vcr_host::{host_input_commitment, verify_strict};

fn env_for(values: &[i32]) -> ExecutorEnv<'static> {
    ExecutorEnv::builder()
        .write(&values.to_vec())
        .unwrap()
        .build()
        .unwrap()
}

fn real_prove(elf: &[u8], values: &[i32]) -> Result<risc0_zkvm::Receipt, String> {
    // dev_mode = false -> genuine STARK seal.
    let opts = ProverOpts::fast().with_dev_mode(false);
    default_prover()
        .prove_with_opts(env_for(values), elf, &opts)
        .map(|info| info.receipt)
        .map_err(|e| format!("{e:#}"))
}

#[test]
#[ignore = "generates a real STARK proof; slow on CPU"]
fn real_composite_proof_verifies_and_binds_result() {
    let values: Vec<i32> = vec![7, -3, 10000, 0, 42, -10000, 5, 9, -1];

    // Execute (this is the "execution" timing reference).
    let session = default_executor()
        .execute(env_for(&values), MEDIAN_GUEST_ELF)
        .expect("execute");
    println!("user cycles = {}", session.cycles());

    // Prove for real.
    let receipt = real_prove(MEDIAN_GUEST_ELF, &values).expect("REAL proving must succeed");
    assert!(
        !matches!(receipt.inner, InnerReceipt::Fake(_)),
        "receipt must be a real seal, got Fake"
    );
    assert!(receipt.seal_size() > 0, "real seal must have non-zero size");
    println!("seal bytes = {}", receipt.seal_size());

    // Strict host verification with the pinned program ID.
    let journal = verify_strict(&receipt, MEDIAN_GUEST_ID, &values).expect("strict verification");
    let decoded: JournalData = receipt.journal.decode().unwrap();
    assert_eq!(decoded, journal);
    assert_eq!(journal.length, values.len() as u32);
    assert_eq!(journal.median, 5);
    assert_eq!(journal.input_commitment, host_input_commitment(&values));
}

#[test]
#[ignore = "generates a real STARK proof; slow on CPU"]
fn real_proof_from_different_program_id_is_rejected() {
    let values: Vec<i32> = vec![1, 2, 3, 4];
    // Honest proof of the DECOY program...
    let receipt = real_prove(DECOY_GUEST_ELF, &values).expect("decoy proves for real");
    // ...must not verify as the MEDIAN program.
    let err = verify_strict(&receipt, MEDIAN_GUEST_ID, &values)
        .expect_err("image-ID mismatch must be rejected");
    println!("got expected error: {err:#}");
    // Sanity: it DOES verify under its own program ID.
    assert!(verify_strict(&receipt, DECOY_GUEST_ID, &values).is_ok());
}

#[test]
#[ignore = "generates a real STARK proof; slow on CPU"]
fn real_proof_with_tampered_journal_is_rejected() {
    let values: Vec<i32> = vec![3, 1, 4, 1, 5, 9, 2, 6];
    let mut receipt = real_prove(MEDIAN_GUEST_ELF, &values).expect("prove");

    // Baseline: the pristine receipt verifies.
    assert!(verify_strict(&receipt, MEDIAN_GUEST_ID, &values).is_ok());

    // Mutate one journal byte after sealing.
    let idx = receipt.journal.bytes.len() / 2;
    receipt.journal.bytes[idx] ^= 0xff;

    // Strict journal decode/binding must reject it. (A cryptographic
    // JournalDigestMismatch occurs inside verify_with_context before our
    // binding check is even reached.)
    let err = verify_strict(&receipt, MEDIAN_GUEST_ID, &values)
        .expect_err("tampered journal must be rejected");
    println!("got expected error: {err:#}");
}

#[test]
#[ignore = "guest faults before proving; fast but grouped with real tests"]
fn real_path_rejects_out_of_range_and_empty() {
    for (label, bad) in [
        ("out-of-range-high", vec![1i32, 10_001]),
        ("out-of-range-low", vec![-10_001]),
        ("empty", vec![]),
        ("too-long", vec![0i32; 257]),
    ] {
        let exec = default_executor().execute(env_for(&bad), MEDIAN_GUEST_ELF);
        assert!(exec.is_err(), "{label}: guest must fault at execution");
        let proven = real_prove(MEDIAN_GUEST_ELF, &bad);
        assert!(proven.is_err(), "{label}: no receipt must ever be produced");
        println!("{label}: rejected as expected ({})", proven.unwrap_err());
    }
}

#[test]
#[ignore = "generates a real STARK proof; slow on CPU"]
fn real_proof_over_256_elements() {
    // Deterministic pseudo-pattern within range.
    let values: Vec<i32> = (0..256).map(|i| ((i * 37) % 20001) - 10000).collect();
    let receipt = real_prove(MEDIAN_GUEST_ELF, &values).expect("prove 256 batch");
    let journal = verify_strict(&receipt, MEDIAN_GUEST_ID, &values).expect("verify");
    assert_eq!(journal.length, 256);
    println!("256-element median = {}", journal.median);
}
