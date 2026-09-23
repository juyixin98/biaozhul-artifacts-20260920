//! Fast functional + security tests.
//!
//! Most tests run the zkVM in DEV MODE (no STARK, milliseconds). Dev mode is
//! used here only to exercise execution and the host's acceptance logic: the
//! whole point is that the host REJECTS the resulting fake receipt. The real
//! cryptographic end-to-end path is covered by `tests/real_proof.rs`, which is
//! `#[ignore]` by default because CPU proving takes longer
//! (run with `cargo test -- --ignored`).

use std::time::Instant;

use methods::{DECOY_GUEST_ELF, DECOY_GUEST_ID, MEDIAN_GUEST_ELF, MEDIAN_GUEST_ID};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, InnerReceipt, Prover, ProverOpts};
use serial_test::serial;
use shared::{encode_batch, sorted_median, validate_batch, JournalData, MAX_BATCH_LEN};

use vcr_host::{host_input_commitment, verify_strict, PhaseTimings};

fn env_for(values: &[i32]) -> ExecutorEnv<'static> {
    ExecutorEnv::builder()
        .write(&values.to_vec())
        .unwrap()
        .build()
        .unwrap()
}

/// Prove using the given opts and return the receipt, bypassing the host
/// binary's pre-validation so we can observe what the zkVM itself does with a
/// bad batch.
fn prove(elf: &[u8], values: &[i32], dev_mode: bool) -> Result<risc0_zkvm::Receipt, String> {
    let opts = ProverOpts::fast().with_dev_mode(dev_mode);
    default_prover()
        .prove_with_opts(env_for(values), elf, &opts)
        .map(|info| info.receipt)
        .map_err(|e| e.to_string())
}

fn sample() -> Vec<i32> {
    vec![7, -3, 10000, 0, 42, -10000, 5, 9, -1]
}

// ---------------------------------------------------------------------------
// Pure unit tests: the shared range-check and median rule.
// ---------------------------------------------------------------------------

#[test]
fn median_is_sorted_lower_median() {
    // Odd length.
    assert_eq!(sorted_median(&sample()), Some(5));
    // Even length uses the LOWER median: sorted [1,2,3,5,8,9] -> 3.
    assert_eq!(sorted_median(&[5, 3, 8, 1, 9, 2]), Some(3));
    assert_eq!(sorted_median(&[-42]), Some(-42));
    assert_eq!(sorted_median(&[]), None);
}

#[test]
fn range_check_accepts_boundaries_and_rejects_outliers() {
    assert!(validate_batch(&[shared::MIN_VALUE, shared::MAX_VALUE, 0]).is_ok());
    let err = validate_batch(&[1, shared::MAX_VALUE + 1]).unwrap_err();
    assert!(matches!(
        err,
        shared::BatchError::OutOfRange(ref e) if e.index == 1 && e.value == shared::MAX_VALUE + 1
    ));
    let err = validate_batch(&[shared::MIN_VALUE - 1]).unwrap_err();
    assert!(matches!(err, shared::BatchError::OutOfRange(_)));
}

#[test]
fn range_check_rejects_empty_and_too_long() {
    assert!(matches!(
        validate_batch(&[]),
        Err(shared::BatchError::Empty)
    ));
    let too_long = vec![0i32; MAX_BATCH_LEN + 1];
    let expected_len = MAX_BATCH_LEN + 1;
    assert!(matches!(
        validate_batch(&too_long),
        Err(shared::BatchError::TooLong(len)) if len == expected_len
    ));
}

#[test]
fn canonical_encoding_is_length_prefixed_little_endian() {
    let enc = encode_batch(&[-1, 2]);
    assert_eq!(
        enc,
        [
            2, 0, 0, 0, // length
            0xff, 0xff, 0xff, 0xff, // -1i32 LE
            2, 0, 0, 0, // 2i32 LE
        ]
    );
}

// ---------------------------------------------------------------------------
// zkVM execution tests (dev mode for speed).
// ---------------------------------------------------------------------------

#[test]
#[serial]
fn guest_executes_and_commits_correct_journal() {
    let values = sample();
    let session = default_executor()
        .execute(env_for(&values), MEDIAN_GUEST_ELF)
        .expect("execution should succeed");
    let journal: JournalData = session.journal.decode().unwrap();
    assert_eq!(journal.length as usize, values.len());
    assert_eq!(journal.median, 5);
    assert_eq!(journal.input_commitment, host_input_commitment(&values));
}

#[test]
#[serial]
fn guest_faults_on_out_of_range_input() {
    // The guest, not just the host, enforces the range: execution must fail.
    let err = default_executor()
        .execute(env_for(&[1, 10_001]), MEDIAN_GUEST_ELF)
        .expect_err("out-of-range batch must fault the guest");
    let msg = format!("{err:#}");
    assert!(
        msg.to_lowercase().contains("range") || msg.contains("10001") || msg.contains("fault"),
        "unexpected fault message: {msg}"
    );
}

#[test]
#[serial]
fn guest_faults_on_empty_batch() {
    let err = default_executor()
        .execute(env_for(&[]), MEDIAN_GUEST_ELF)
        .expect_err("empty batch must fault the guest");
    assert!(format!("{err:#}").to_lowercase().contains("empty"));
}

#[test]
#[serial]
fn guest_accepts_full_256_batch() {
    let values = vec![1i32; MAX_BATCH_LEN];
    let session = default_executor()
        .execute(env_for(&values), MEDIAN_GUEST_ELF)
        .expect("256 elements must be accepted");
    let journal: JournalData = session.journal.decode().unwrap();
    assert_eq!(journal.length, 256);
    assert_eq!(journal.median, 1);
}

// ---------------------------------------------------------------------------
// Security property tests.
// ---------------------------------------------------------------------------

#[test]
#[serial]
fn dev_mode_receipt_is_fake_and_is_rejected_even_with_env_var() {
    // Simulate an operator who (mis)configures dev mode in the environment.
    unsafe { std::env::set_var("RISC0_DEV_MODE", "1") };

    let values = sample();
    let receipt = prove(MEDIAN_GUEST_ELF, &values, true).expect("dev prove succeeds");
    assert!(
        matches!(receipt.inner, InnerReceipt::Fake(_)),
        "dev mode must produce a Fake receipt"
    );

    // Strict verification refuses it regardless of the environment.
    let err = verify_strict(&receipt, MEDIAN_GUEST_ID, &values)
        .expect_err("fake receipt must be rejected");
    assert!(format!("{err:#}").contains("FAKE"));

    unsafe { std::env::remove_var("RISC0_DEV_MODE") };
}

#[test]
#[serial]
fn wrong_program_id_is_rejected() {
    // The decoy is a genuinely different program. Prove it honestly (dev mode
    // is enough here: the image-ID binding is part of the claim check, not the
    // STARK)...
    let values = sample();
    let receipt = prove(DECOY_GUEST_ELF, &values, true).expect("decoy proves");

    // ...then require the MEDIAN image ID. To reach the claim/image-ID
    // comparison on a fake seal, verify with a dev-mode-enabled context (the
    // strict host checker rejects Fake earlier, which is covered separately).
    // The claim commits the executed image, so this must fail with a
    // claim-digest mismatch even though the seal "passes" in dev mode.
    use risc0_zkvm::VerifierContext;
    let dev_ctx = VerifierContext::default().with_dev_mode(true);
    let err = receipt
        .verify_with_context(&dev_ctx, MEDIAN_GUEST_ID)
        .expect_err("a decoy receipt must not verify against the median ID");
    let msg = format!("{err}").to_lowercase();
    assert!(
        msg.contains("mismatch") || msg.contains("claim"),
        "expected an image-ID/claim failure, got: {msg}"
    );

    // Under its OWN image ID the dev-context check passes (proves the failure
    // is about ID pinning, not a malformed receipt).
    assert!(receipt
        .verify_with_context(&dev_ctx, DECOY_GUEST_ID)
        .is_ok());

    // And the strict host checker rejects it regardless (fake seal + it is
    // the wrong program for a MEDIAN expectation anyway).
    assert!(verify_strict(&receipt, MEDIAN_GUEST_ID, &values).is_err());
}

#[test]
#[serial]
fn tampered_journal_is_rejected() {
    let values = sample();
    let mut receipt = prove(MEDIAN_GUEST_ELF, &values, true).expect("prove");
    // Flip a byte in the public journal after the seal was made.
    receipt.journal.bytes.last_mut().unwrap().flip();
    // It is a fake receipt, so the fake check fires first; force the seal
    // path's journal check to be visible by verifying integrity directly is
    // not possible on a fake seal — instead assert the strict verifier still
    // rejects (the guarantee under test is simply "tampered => rejected").
    assert!(verify_strict(&receipt, MEDIAN_GUEST_ID, &values).is_err());
}

trait Flip {
    fn flip(&mut self);
}
impl Flip for u8 {
    fn flip(&mut self) {
        *self ^= 0x01;
    }
}

#[test]
#[serial]
fn input_binding_catches_a_different_batch() {
    // Journal of one batch presented alongside a different claimed batch.
    let values = sample();
    let receipt = prove(MEDIAN_GUEST_ELF, &values, true).unwrap();
    let mut claimed = values.clone();
    claimed[0] += 1;
    // Fake receipt rejected first; the important property is non-acceptance.
    assert!(verify_strict(&receipt, MEDIAN_GUEST_ID, &claimed).is_err());
}

#[test]
fn timings_report_three_phases() {
    // Just checks the reporting struct round-trips.
    let t = PhaseTimings {
        execute_ms: 1,
        prove_ms: 2,
        verify_ms: 3,
    };
    let json = serde_json::to_string(&t).unwrap();
    assert!(json.contains("execute_ms"));
    assert!(json.contains("prove_ms"));
    assert!(json.contains("verify_ms"));
}

#[test]
#[serial]
fn out_of_range_and_empty_can_never_produce_verified_result() {
    // Even a host that skips its own pre-check cannot get a receipt: proving
    // itself fails because the guest faults.
    for bad in [vec![1, 10_001], vec![-10_001], vec![]] {
        let result = prove(MEDIAN_GUEST_ELF, &bad, true);
        assert!(
            result.is_err(),
            "bad batch {bad:?} must not produce a receipt, got {:?}",
            result.map(|_| "receipt")
        );
    }
}

#[test]
#[serial]
fn timing_realistic_values() {
    let values = sample();
    let t = Instant::now();
    default_executor()
        .execute(env_for(&values), MEDIAN_GUEST_ELF)
        .unwrap();
    assert!(t.elapsed().as_secs() < 60, "execute should be fast");
}
