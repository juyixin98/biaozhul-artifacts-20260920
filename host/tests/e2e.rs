//! End-to-end integration tests for the verifiable batch-median sample.
//!
//! These tests exercise the host's admission logic directly (fast, in-process)
//! for the four mandated adversarial cases: changed program image ID; tampered
//! journal; out-of-range inputs; and an empty batch. They also cover the
//! security-critical property that a dev-mode fake receipt can never pass the
//! default verifier.
//!
//! Tests marked `#[ignore] = "real_proof"` additionally produce and verify a
//! real STARK receipt; they are slow and are run explicitly via
//! `cargo test -- --ignored` (or the `acceptance` wrapper).

use batch_median_core::{input_commitment, Journal};
use batch_median_host::{flavor_of, prove, prove_unchecked, verify_and_admit, ReceiptFlavor};
use batch_median_methods::image_id;

const VALID: &[i64] = &[9, -1, 1_000_000, 0, 42, -512, 777, 256];

// ---- dev-mode receipt: fast, available in every build --------------------

fn prove_dev(values: &[i64]) -> risc0_zkvm::Receipt {
    prove(values, true)
        .expect("dev prove should succeed")
        .receipt
}

#[test]
fn fake_receipt_is_rejected_by_default_verifier() {
    let receipt = prove_dev(VALID);
    assert_eq!(flavor_of(&receipt), ReceiptFlavor::Fake);

    // The core security requirement: no explicit dev opt-in => rejection,
    // regardless of RISC0_DEV_MODE in the surrounding environment.
    let err = verify_and_admit(&receipt, image_id(), VALID, false).unwrap_err();
    let msg = format!("{err:#}");
    assert!(
        msg.contains("development") || msg.contains("FakeReceipt"),
        "rejection must name the development receipt, got: {msg}"
    );
}

#[test]
fn fake_receipt_is_accepted_only_with_explicit_opt_in() {
    let receipt = prove_dev(VALID);
    let out = verify_and_admit(&receipt, image_id(), VALID, true)
        .expect("explicit dev mode should admit the fake receipt");
    assert_eq!(out.flavor, ReceiptFlavor::Fake);
    assert_eq!(out.journal.median, 9);
}

// ---- (1) changed program ID ----------------------------------------------

#[test]
fn changed_image_id_is_rejected() {
    let receipt = prove_dev(VALID);
    let mut wrong = image_id();
    // Flip a byte in the expected image ID: verify must fail on claim mismatch.
    let bytes: &mut [u8] = wrong.as_mut_bytes();
    bytes[0] ^= 0xff;

    let err = verify_and_admit(&receipt, wrong, VALID, true).unwrap_err();
    let msg = format!("{err:#}");
    assert!(
        msg.contains("verification failed"),
        "wrong image ID must fail cryptographic verification, got: {msg}"
    );
}

// ---- (2) tampered journal -------------------------------------------------

#[test]
fn tampered_journal_is_rejected() {
    let mut receipt = prove_dev(VALID);
    // Flip the low byte of the committed median (journal offset 16).
    receipt.journal.bytes[16] ^= 0x01;

    let err = verify_and_admit(&receipt, image_id(), VALID, true).unwrap_err();
    let msg = format!("{err:#}");
    assert!(
        msg.contains("verification failed"),
        "tampered journal must break the seal check, got: {msg}"
    );
}

#[test]
fn tampered_commitment_is_rejected() {
    let mut receipt = prove_dev(VALID);
    // Flip a byte inside the commitment region (offset 24+).
    receipt.journal.bytes[30] ^= 0x80;
    assert!(verify_and_admit(&receipt, image_id(), VALID, true).is_err());
}

// ---- binding: receipt for a different batch ------------------------------

#[test]
fn receipt_does_not_bind_to_different_inputs() {
    let receipt = prove_dev(VALID);
    let other = &[9, -1, 1_000_000, 0, 42, -512, 777, 257]; // last value changed
    let err = verify_and_admit(&receipt, image_id(), other, true).unwrap_err();
    let msg = format!("{err:#}");
    assert!(
        msg.contains("binding"),
        "a receipt for one batch must not verify against another, got: {msg}"
    );
}

#[test]
fn reordered_inputs_have_distinct_commitments() {
    let a = vec![1i64, 2, 3];
    let b = vec![3i64, 2, 1];
    assert_ne!(input_commitment(&a), input_commitment(&b));
}

// ---- (3) out-of-range inputs and (4) empty batch --------------------------
// These never reach the prover: host pre-flight rejects them, and the guest
// independently enforces the same rules.

#[test]
fn out_of_range_inputs_rejected_pre_flight() {
    let too_big = vec![1, 1_000_001];
    let too_small = vec![-1_000_001];
    assert!(prove(&too_big, true).is_err());
    assert!(prove(&too_small, true).is_err());
}

#[test]
fn empty_batch_rejected_pre_flight() {
    assert!(prove(&[], true).is_err());
}

#[test]
fn oversized_batch_rejected_pre_flight() {
    let big = vec![0i64; 257];
    assert!(prove(&big, true).is_err());
}

// ---- journal structure ----------------------------------------------------

#[test]
fn malformed_journal_bytes_are_rejected() {
    assert!(Journal::decode(&[]).is_err());
    assert!(Journal::decode(&[0u8; 56]).is_err()); // bad magic
    let mut good = Journal::new(VALID, 9).encode();
    good[4] = 99; // bad version
    assert!(Journal::decode(&good).is_err());
    let mut bad_len = Journal::new(VALID, 9).encode();
    bad_len[8..16].copy_from_slice(&0u64.to_le_bytes()); // length 0
    assert!(Journal::decode(&bad_len).is_err());
}

// ---- boundary batches -----------------------------------------------------

#[test]
fn boundary_values_and_max_length_admitted() {
    let max = vec![500i64; 256];
    let receipt = prove_dev(&max);
    let out = verify_and_admit(&receipt, image_id(), &max, true).unwrap();
    assert_eq!(out.journal.median, 500);
    assert_eq!(out.journal.length, 256);

    let bounds = vec![-1_000_000i64, 1_000_000];
    let r2 = prove_dev(&bounds);
    let out2 = verify_and_admit(&r2, image_id(), &bounds, true).unwrap();
    // even-length tie-break documented: lower of the two middles
    assert_eq!(out2.journal.median, -1_000_000);
}

// ---- REAL STARK proof -----------------------------------------------------
// Ignored by default because proving takes tens of seconds. Run with:
//   cargo test --release --test e2e -- --ignored --test-threads=1

#[test]
#[ignore = "real_proof: generates a real STARK receipt, slow"]
fn real_stark_receipt_is_verified_and_admitted() {
    let proven = prove(VALID, false).expect("real proving must succeed");
    assert_ne!(flavor_of(&proven.receipt), ReceiptFlavor::Fake);

    // A genuine proof verifies WITHOUT any dev opt-in.
    let out = verify_and_admit(&proven.receipt, image_id(), VALID, false)
        .expect("real receipt must verify under the strict default path");
    assert_eq!(out.journal.median, 9);
    assert!(matches!(
        out.flavor,
        ReceiptFlavor::Composite | ReceiptFlavor::Succinct
    ));

    // And it survives all the same adversarial checks.
    let mut tampered = proven.receipt.clone();
    tampered.journal.bytes[16] ^= 0x01;
    assert!(verify_and_admit(&tampered, image_id(), VALID, false).is_err());

    let mut wrong_id = image_id();
    wrong_id.as_mut_bytes()[7] ^= 0x01;
    assert!(verify_and_admit(&proven.receipt, wrong_id, VALID, false).is_err());
}

#[test]
#[ignore = "real_proof: a real proof must also reject guest-invalid inputs"]
fn real_proof_rejects_out_of_range_and_empty() {
    // Skip host pre-flight so the inputs reach the zkVM: the guest panics, and
    // with default opts the prover refuses to turn that into a success receipt.
    assert!(prove_unchecked(&[2_000_000], false).is_err());
    assert!(prove_unchecked(&[], false).is_err());
    assert!(prove_unchecked(&vec![0i64; 257], false).is_err());
}
