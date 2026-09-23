//! Execution / proving / verification pipeline with separate timing.

use std::time::Instant;

use anyhow::{bail, Context, Result};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, Prover, ProverOpts};
use shared::{sorted_median, validate_batch, JournalData};

use crate::{
    io::{host_input_commitment, image_id_hex, PhaseTimings, VerifiedOutput},
    verify::{receipt_kind_name, verify_strict},
};

/// Outcome of a run. `Rejected` carries the reason; a dev-mode receipt is
/// always reported as rejected rather than as a verified result.
pub enum RunOutcome {
    Verified(Box<VerifiedOutput>),
    /// The receipt was produced (possibly in dev mode) but failed the host's
    /// strict acceptance rules.
    Rejected(String),
}

/// Run the full pipeline for one batch.
///
/// * `elf` / `image_id` — the guest program to run and the ID we will require
///   the receipt to verify against.
/// * `values` — the input batch.
/// * `dev_mode` — prover option. When `true` the zkVM emits a fake,
///   non-cryptographic receipt, which the verifier then REJECTS. This exists
///   to demonstrate that dev mode can never masquerade as a real proof.
/// * `receipt_path` — optional path to persist the bincode-serialized receipt.
#[allow(clippy::too_many_arguments)]
pub fn run_pipeline(
    elf: &[u8],
    image_id: [u32; 8],
    values: &[i32],
    dev_mode: bool,
    receipt_path: Option<&str>,
    label: &str,
) -> Result<RunOutcome> {
    // Host-side pre-validation mirrors the guest so that obvious bad inputs
    // are reported cleanly before spending any cycles. The guest re-checks
    // independently, so a tampered host binary cannot weaken the guarantee.
    if let Err(err) = validate_batch(values) {
        bail!("batch rejected by host validation: {err:?}");
    }

    println!("[{label}] batch length = {}", values.len());
    println!(
        "[{label}] host input commitment = {}",
        hex::encode(host_input_commitment(values))
    );
    if let Some(median) = sorted_median(values) {
        println!("[{label}] host-computed median  = {median}");
    }

    let env_builder = || {
        ExecutorEnv::builder()
            .write(&values.to_vec())
            .context("failed to write input to executor env")
            .and_then(|b| b.build().context("failed to build executor env"))
    };

    // ---- Phase 1: execute only (no proving) --------------------------------
    let exec_env = env_builder()?;
    let t = Instant::now();
    let session = default_executor()
        .execute(exec_env, elf)
        .context("execution FAILED (guest faulted)")?;
    let execute_ms = t.elapsed().as_millis();
    println!(
        "[{label}] execute: {execute_ms} ms, user cycles = {}, exit = {:?}",
        session.cycles(),
        session.exit_code
    );

    // ---- Phase 2: prove ----------------------------------------------------
    let opts = ProverOpts::fast().with_dev_mode(dev_mode); // composite, fastest CPU proof
    let prove_env = env_builder()?;
    let t = Instant::now();
    let prove_info = default_prover()
        .prove_with_opts(prove_env, elf, &opts)
        .context("proving FAILED")?;
    let prove_ms = t.elapsed().as_millis();
    let receipt = prove_info.receipt;
    let stats = prove_info.stats;
    println!(
        "[{label}] prove:   {prove_ms} ms, segments = {}, total cycles = {}, receipt = {}",
        stats.segments,
        stats.total_cycles,
        receipt_kind_name(&receipt)
    );

    if let Some(path) = receipt_path {
        let bytes = bincode::serialize(&receipt).context("failed to serialize receipt")?;
        std::fs::write(path, bytes).context("failed to write receipt file")?;
        println!("[{label}] receipt written to {path}");
    }

    // ---- Phase 3: strict verification --------------------------------------
    let t = Instant::now();
    let verified: Result<JournalData> = verify_strict(&receipt, image_id, values);
    let verify_ms = t.elapsed().as_millis();

    match verified {
        Err(reason) => {
            println!("[{label}] verify:  {verify_ms} ms -> REJECTED");
            Ok(RunOutcome::Rejected(reason.to_string()))
        }
        Ok(journal) => {
            println!("[{label}] verify:  {verify_ms} ms -> OK");
            let out = VerifiedOutput {
                verified: true,
                image_id: image_id_hex(image_id),
                receipt_kind: receipt_kind_name(&receipt).to_string(),
                seal_bytes: receipt.seal_size(),
                length: journal.length,
                median: journal.median,
                input_commitment: hex::encode(journal.input_commitment),
                timings: PhaseTimings {
                    execute_ms,
                    prove_ms,
                    verify_ms,
                },
                segments: stats.segments,
                total_cycles: stats.total_cycles,
                user_cycles: stats.user_cycles,
                receipt_path: receipt_path.map(str::to_string),
            };
            Ok(RunOutcome::Verified(Box::new(out)))
        }
    }
}
