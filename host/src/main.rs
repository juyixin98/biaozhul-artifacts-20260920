//! `batch-median` — verifiable local integer batch computation on RISC Zero.
//!
//! Subcommands:
//!   - `execute`: run the guest, no proof. Fast functional check with timing.
//!   - `prove`: execute + produce a real STARK receipt; verify it against the
//!     expected image ID and only then return the verified median.
//!   - `verify`: independently verify a saved receipt file.
//!
//! `--dev-mode` is a deliberate, loudly-labeled escape hatch that produces
//! FAKE receipts. They are rejected by the default verification path and can
//! never be mistaken for real proofs.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use anyhow::{Context, Result};
use batch_median_core::Journal;
use batch_median_host::{
    admit, execute, execute_unchecked, flavor_of, image_id_hex, journal_summary, prove,
    prove_unchecked, verify_and_admit, BatchFile, CycleStats, ReceiptEnvelope, ResultReport,
    Timings,
};
use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(
    name = "batch-median",
    version,
    about = "Verifiable batch range-check + median on the RISC Zero zkVM",
    long_about = None
)]
struct Cli {
    /// Produce/accept development (fake) receipts. Off by default. The program
    /// exits with an error if a fake receipt reaches the default verifier.
    ///
    /// Note: when built with `--features strict`, dev mode is disabled
    /// compile-time and setting this flag makes the zkVM refuse to run.
    #[arg(long, global = true)]
    dev_mode: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Execute the guest without generating a proof.
    Execute {
        /// Path to a JSON file {"values": [..256 signed ints]}.
        input: PathBuf,
        /// Skip host-side pre-flight validation so the guest's own enforcement
        /// is exercised. Invalid inputs must make the guest fault.
        #[arg(long)]
        unsafe_skip_preflight: bool,
    },

    /// Execute, generate a receipt, verify against the expected image ID, and
    /// print the verified result. Saves the receipt unless --no-save.
    Prove {
        input: PathBuf,
        /// Skip host-side pre-flight validation (self-test of guest enforcement).
        #[arg(long)]
        unsafe_skip_preflight: bool,
        /// Where to write the receipt envelope (JSON). Default: alongside input.
        #[arg(long)]
        receipt_out: Option<PathBuf>,
        /// Do not persist the receipt.
        #[arg(long)]
        no_save: bool,
        /// Write the machine-readable result report here as well as stdout.
        #[arg(long)]
        report_out: Option<PathBuf>,
    },

    /// Verify a previously produced receipt envelope.
    Verify {
        /// Receipt envelope JSON produced by `prove`.
        receipt: PathBuf,
        /// Verify against a caller-supplied expected image ID (hex) instead of
        /// the image ID embedded in this binary. Used to demonstrate that a
        /// changed program ID is rejected.
        #[arg(long)]
        expect_image_id: Option<String>,
        /// Tamper the journal before verification (demo/self-test only):
        /// rewrites the median byte in the receipt journal.
        #[arg(long)]
        tamper_journal: bool,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(report) => {
            print_report(&report);
            if report.verified {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            }
        }
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn run(cli: Cli) -> Result<ResultReport> {
    let image_hex = image_id_hex();
    match cli.command {
        Command::Execute {
            input,
            unsafe_skip_preflight,
        } => {
            let batch = BatchFile::load(&input)?;
            let exec = if unsafe_skip_preflight {
                execute_unchecked(&batch.values)
            } else {
                execute(&batch.values)
            }?;
            let journal_bytes = hex::decode(&exec.journal_hex)?;
            let journal = Journal::decode(&journal_bytes)
                .map_err(|e| anyhow::anyhow!("decoding journal: {}", e.as_str()))?;
            let (median, commit) = journal_summary(&journal);
            Ok(ResultReport {
                verified: false,
                image_id_hex: image_hex,
                dev_mode: false,
                receipt_flavor: "none (execution only)".into(),
                input_length: batch.values.len(),
                median: Some(median),
                input_commitment_hex: Some(commit),
                journal_hex: Some(exec.journal_hex),
                receipt_path: None,
                timings_ms: Timings {
                    execution_ms: exec.elapsed_ms,
                    ..Default::default()
                },
                cycles: CycleStats {
                    user: exec.user_cycles,
                    segments: exec.segments as u64,
                    ..Default::default()
                },
                note: Some("execution only; no receipt produced, result is NOT verified".into()),
            })
        }

        Command::Prove {
            input,
            unsafe_skip_preflight,
            receipt_out,
            no_save,
            report_out,
        } => {
            let batch = BatchFile::load(&input)?;
            let total = Instant::now();

            let proven = if unsafe_skip_preflight {
                prove_unchecked(&batch.values, cli.dev_mode)
            } else {
                prove(&batch.values, cli.dev_mode)
            }?;

            // Host admission: only after this returns Ok is the result trusted.
            let (admitted, verification_ms) = admit(&proven.receipt, &batch.values, cli.dev_mode)?;

            let journal = admitted.journal;
            let (median, commit) = journal_summary(&journal);
            let journal_hex = hex::encode(&proven.receipt.journal.bytes);

            let receipt_path = if no_save {
                None
            } else {
                let path = receipt_out.unwrap_or_else(|| {
                    let mut p = input.clone();
                    p.set_extension("receipt.json");
                    p
                });
                let envelope = ReceiptEnvelope::pack(cli.dev_mode, &batch.values, &proven.receipt)?;
                std::fs::write(&path, serde_json::to_string_pretty(&envelope)?)
                    .with_context(|| format!("writing {}", path.display()))?;
                Some(path.display().to_string())
            };

            let flavor = admitted.flavor.as_str();

            let report = ResultReport {
                verified: true,
                image_id_hex: image_hex,
                dev_mode: cli.dev_mode,
                receipt_flavor: flavor.into(),
                input_length: batch.values.len(),
                median: Some(median),
                input_commitment_hex: Some(commit),
                journal_hex: Some(journal_hex),
                receipt_path,
                timings_ms: Timings {
                    execution_ms: proven.execution_ms,
                    proving_ms: proven.proving_ms,
                    verification_ms,
                    total_ms: total.elapsed().as_millis(),
                },
                cycles: CycleStats {
                    total: proven.total_cycles,
                    user: proven.user_cycles,
                    segments: proven.segments,
                },
                note: if cli.dev_mode {
                    Some(
                        "DEV MODE: receipt is a FAKE placeholder, has no cryptographic integrity, \
                         and is rejected by normal verification"
                            .into(),
                    )
                } else {
                    Some("real STARK receipt; verified against the embedded image ID".into())
                },
            };

            if let Some(p) = report_out {
                std::fs::write(&p, serde_json::to_string_pretty(&report)?)
                    .with_context(|| format!("writing {}", p.display()))?;
            }
            Ok(report)
        }

        Command::Verify {
            receipt,
            expect_image_id,
            tamper_journal,
        } => {
            let envelope: ReceiptEnvelope = serde_json::from_str(
                &std::fs::read_to_string(&receipt)
                    .with_context(|| format!("reading {}", receipt.display()))?,
            )
            .context("parsing receipt envelope")?;

            let mut r = envelope.unpack()?;

            // Demo self-test: mutate one journal byte after seal creation.
            // Receipt::verify binds the journal digest, so this must fail.
            if tamper_journal {
                if r.journal.bytes.len() < 17 {
                    anyhow::bail!("journal too short to tamper");
                }
                // Flip low byte of the i64 median field (offset 16).
                r.journal.bytes[16] ^= 0x01;
            }

            let expected = match expect_image_id {
                Some(h) => parse_digest(&h)?,
                None => batch_median_methods::image_id(),
            };

            // Verification NEVER auto-trusts the envelope's dev_mode flag for
            // admission decisions; dev acceptance requires the explicit CLI
            // flag, and defaults to false.
            let start = Instant::now();
            let admitted = verify_and_admit(&r, expected, &envelope.values, cli.dev_mode);
            let verification_ms = start.elapsed().as_millis();

            match admitted {
                Ok(out) => {
                    let (median, commit) = journal_summary(&out.journal);
                    let flavor = out.flavor.as_str();
                    Ok(ResultReport {
                        verified: true,
                        image_id_hex: hex::encode(expected.as_bytes()),
                        dev_mode: cli.dev_mode,
                        receipt_flavor: flavor.into(),
                        input_length: envelope.values.len(),
                        median: Some(median),
                        input_commitment_hex: Some(commit),
                        journal_hex: Some(hex::encode(&r.journal.bytes)),
                        receipt_path: Some(receipt.display().to_string()),
                        timings_ms: Timings {
                            verification_ms,
                            ..Default::default()
                        },
                        cycles: CycleStats::default(),
                        note: Some("receipt verified and inputs bound".into()),
                    })
                }
                Err(e) => Ok(ResultReport {
                    verified: false,
                    image_id_hex: hex::encode(expected.as_bytes()),
                    dev_mode: cli.dev_mode,
                    receipt_flavor: flavor_of(&r).as_str().into(),
                    input_length: envelope.values.len(),
                    median: None,
                    input_commitment_hex: None,
                    journal_hex: Some(hex::encode(&r.journal.bytes)),
                    receipt_path: Some(receipt.display().to_string()),
                    timings_ms: Timings {
                        verification_ms,
                        ..Default::default()
                    },
                    cycles: CycleStats::default(),
                    note: Some(format!("VERIFICATION REJECTED: {e:#}")),
                }),
            }
        }
    }
}

fn parse_digest(hex_str: &str) -> Result<risc0_zkvm::Digest> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    let bytes = hex::decode(s).context("expected image id as hex")?;
    if bytes.len() != 32 {
        anyhow::bail!(
            "image id must be 32 bytes (64 hex chars), got {}",
            bytes.len()
        );
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&bytes);
    Ok(arr.into())
}

fn print_report(r: &ResultReport) {
    match serde_json::to_string_pretty(r) {
        Ok(text) => println!("{text}"),
        Err(e) => eprintln!("error: failed to serialize report: {e}"),
    }
}
