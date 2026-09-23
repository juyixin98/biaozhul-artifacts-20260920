//! Execution / proving / verification orchestration.
//!
//! The three costly phases are timed separately:
//! * [`execute`] runs the guest without producing a proof;
//! * [`prove`] produces a real STARK receipt (or a fake one in dev mode);
//! * [`admit`] cryptographically verifies the receipt and binds it to inputs.

use std::time::Instant;

use anyhow::{bail, Context, Result};
use batch_median_core::validate;
use batch_median_methods::{image_id, BATCH_MEDIAN_ELF};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, ProverOpts, Receipt};

use crate::verifier::{verify_and_admit, VerifiedOutput};

/// Build the zkVM input environment for one batch. The host writes a `u32`
/// length followed by the `i64` values, matching the guest's `read`/`read_slice`.
fn build_env(values: &[i64]) -> Result<ExecutorEnv<'static>> {
    ExecutorEnv::builder()
        .write(&(values.len() as u32))
        .context("writing batch length to zkVM input")?
        .write_slice(values)
        .build()
        .context("building ExecutorEnv")
}

/// Result of the execution-only phase.
pub struct ExecutionResult {
    pub journal_hex: String,
    pub elapsed_ms: u128,
    pub segments: usize,
    pub user_cycles: u64,
}

/// Execute the guest without proving. Used by the `execute` subcommand and as
/// the first timing stage of `prove`. Invalid batches surface as guest
/// execution errors and are reported honestly.
pub fn execute(values: &[i64]) -> Result<ExecutionResult> {
    validate(values).map_err(|e| anyhow::anyhow!("pre-flight input validation: {}", e.as_str()))?;
    execute_unchecked(values)
}

/// Execute without host-side pre-flight validation. Used to demonstrate that
/// the **guest itself** enforces length/range rules: invalid inputs reach the
/// zkVM and the guest faults instead of committing a journal.
pub fn execute_unchecked(values: &[i64]) -> Result<ExecutionResult> {
    let env = build_env(values)?;
    let start = Instant::now();
    let session = default_executor()
        .execute(env, BATCH_MEDIAN_ELF)
        .context("zkVM execution failed (guest aborted)")?;
    let elapsed_ms = start.elapsed().as_millis();

    // An honest executor reports a non-zero exit instead of pretending success.
    if !matches!(session.exit_code, risc0_zkvm::ExitCode::Halted(0)) {
        bail!("guest exited unsuccessfully: {:?}", session.exit_code);
    }

    let (segments, user_cycles) = (
        session.segments.len(),
        session
            .segments
            .iter()
            .map(|s| s.cycles as u64)
            .sum::<u64>(),
    );

    Ok(ExecutionResult {
        journal_hex: hex::encode(&session.journal.bytes),
        elapsed_ms,
        segments,
        user_cycles,
    })
}

/// Output of the proving phase with separated timings.
pub struct ProveResult {
    pub receipt: Receipt,
    pub execution_ms: u128,
    pub proving_ms: u128,
    pub total_cycles: u64,
    pub user_cycles: u64,
    pub segments: u64,
}

/// Execute then prove. Timings:
/// * `execution_ms` — `default_executor().execute` (no seal),
/// * `proving_ms`   — prover execution + STARK generation.
///
/// `dev_mode` explicitly opts into fake receipts; the default produces a real
/// STARK proof locally.
pub fn prove(values: &[i64], dev_mode: bool) -> Result<ProveResult> {
    validate(values).map_err(|e| anyhow::anyhow!("pre-flight input validation: {}", e.as_str()))?;
    prove_unchecked(values, dev_mode)
}

/// Prove without host-side pre-flight validation, letting the guest enforce the
/// rules inside the zkVM. A faulting guest cannot be proven under the default
/// options (`prove_guest_errors = false`), so invalid batches surface as an
/// error rather than a success receipt.
pub fn prove_unchecked(values: &[i64], dev_mode: bool) -> Result<ProveResult> {
    // ---- phase 1: execution-only timing ----
    let exec = execute_unchecked(values)?;
    let execution_ms = exec.elapsed_ms;

    // ---- phase 2: proving ----
    let env = build_env(values)?;
    let opts = ProverOpts::default().with_dev_mode(dev_mode);
    let start = Instant::now();
    let info = default_prover()
        .prove_with_opts(env, BATCH_MEDIAN_ELF, &opts)
        .context("proving failed")?;
    let proving_ms = start.elapsed().as_millis();

    Ok(ProveResult {
        receipt: info.receipt,
        execution_ms,
        proving_ms,
        total_cycles: info.stats.total_cycles,
        user_cycles: info.stats.user_cycles,
        segments: info.stats.segments as u64,
    })
}

/// Verify a receipt against the embedded expected image ID, bind it to the
/// inputs the host sent, and time the whole verification stage.
pub fn admit(receipt: &Receipt, values: &[i64], allow_dev: bool) -> Result<(VerifiedOutput, u128)> {
    let expected = image_id();
    let start = Instant::now();
    let mut out = verify_and_admit(receipt, expected, values, allow_dev)?;
    let elapsed = start.elapsed().as_millis();
    out.user_cycles = 0;
    Ok((out, elapsed))
}

/// Expected image ID as a hex string (for reports / logs).
pub fn image_id_hex() -> String {
    hex::encode(image_id().as_bytes())
}
