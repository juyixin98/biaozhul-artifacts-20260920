//! `vcr-host` — verifiable computation receipt demo (pure backend).
//!
//! Subcommands:
//!   demo    run example batch, real proof, strict verify (default)
//!   prove   prove a JSON input file and optionally save the receipt
//!   verify  verify a saved receipt against the expected program ID
//!   execute run the executor only (no proof) and report timing/cycles
//!
//! Real cryptography is always used unless `--dev-mode` is passed, and a
//! dev-mode (fake) receipt is then REJECTED by the verifier and reported as
//! such — it can never be returned as a verified result.

use anyhow::{bail, Context, Result};
use methods::{DECOY_GUEST_ELF, DECOY_GUEST_ID, MEDIAN_GUEST_ELF, MEDIAN_GUEST_ID};
use risc0_zkvm::{default_executor, ExecutorEnv};
use shared::validate_batch;
use vcr_host::{
    io::{image_id_hex, read_input},
    pipeline::{run_pipeline, RunOutcome},
    verify, VerifiedOutput,
};

const DEFAULT_INPUT: &str = "inputs/example.json";
const DEFAULT_RECEIPT: &str = "receipts/latest.bin";

struct Args {
    command: String,
    input: String,
    receipt: String,
    dev_mode: bool,
    json: bool,
    /// On `verify`: which program ID to expect.
    expect_decoy: bool,
    /// On `demo`/`prove`: which guest ELF to run ("median" or "decoy").
    /// Always pinned to the matching image ID for verification.
    guest: String,
    /// On `verify`: path to the batch to bind against (default = --input).
    bind_input: Option<String>,
    /// Mutate the loaded journal bytes before verifying (tamper test).
    tamper_journal: bool,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            command: "demo".to_string(),
            input: DEFAULT_INPUT.to_string(),
            receipt: DEFAULT_RECEIPT.to_string(),
            dev_mode: false,
            json: false,
            expect_decoy: false,
            guest: "median".to_string(),
            bind_input: None,
            tamper_journal: false,
        }
    }
}

fn parse_args() -> Args {
    let mut args = Args::default();
    let mut positional: Vec<String> = Vec::new();
    let mut iter = std::env::args().skip(1);
    while let Some(a) = iter.next() {
        match a.as_str() {
            "--dev-mode" => args.dev_mode = true,
            "--json" => args.json = true,
            "--expect-decoy" => args.expect_decoy = true,
            "--tamper-journal" => args.tamper_journal = true,
            "--input" => args.input = iter.next().unwrap_or_default(),
            "--receipt" => args.receipt = iter.next().unwrap_or_default(),
            "--bind" => args.bind_input = iter.next(),
            "--guest" => args.guest = iter.next().unwrap_or_default(),
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            other if !other.starts_with('-') => positional.push(other.to_string()),
            other => {
                eprintln!("warning: ignoring unknown argument `{other}`");
            }
        }
    }
    if let Some(cmd) = positional.first() {
        args.command = cmd.clone();
    }
    args
}

fn print_help() {
    println!(
        "vcr-host — RISC Zero verifiable batch-median receipt\n\
\n\
USAGE:\n\
    vcr-host [demo|prove|verify|execute] [OPTIONS]\n\
\n\
OPTIONS:\n\
    --input <PATH>       JSON input file            (default: {DEFAULT_INPUT})\n\
    --receipt <PATH>     receipt file               (default: {DEFAULT_RECEIPT})\n\
    --bind <PATH>        (verify) batch to bind the journal to\n\
    --dev-mode           produce a fake dev-mode receipt (it will be REJECTED)\n\
    --guest <id>         demo/prove: run the `median` or `decoy` guest\n\
    --expect-decoy       (verify) expect the DECOY program ID instead\n\
    --tamper-journal     (verify) flip a journal byte before verifying\n\
    --json               emit machine-readable JSON for the verified result\n\
    -h, --help           show this help\n"
    );
}

fn main() -> Result<()> {
    let args = parse_args();
    match args.command.as_str() {
        "demo" => cmd_demo(&args),
        "prove" => cmd_prove(&args),
        "verify" => cmd_verify(&args),
        "execute" => cmd_execute(&args),
        other => {
            eprintln!("unknown command `{other}`\n");
            print_help();
            std::process::exit(2);
        }
    }
}

fn report_outcome(outcome: RunOutcome, json: bool) -> Result<()> {
    match outcome {
        RunOutcome::Verified(out_) => {
            let out: VerifiedOutput = *out_;
            if json {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&out).expect("serialize output")
                );
            } else {
                println!("\n=== VERIFIED RESULT (real cryptographic proof) ===");
                println!("image id       : {}", out.image_id);
                println!("receipt kind   : {}", out.receipt_kind);
                println!("seal bytes     : {}", out.seal_bytes);
                println!("batch length   : {}", out.length);
                println!("median         : {}", out.median);
                println!("input commit   : {}", out.input_commitment);
                if let Some(p) = &out.receipt_path {
                    println!("receipt saved  : {p}");
                }
                println!(
                    "timings (ms)   : execute={}, prove={}, verify={}",
                    out.timings.execute_ms, out.timings.prove_ms, out.timings.verify_ms
                );
                println!(
                    "cycles         : total={}, user={}, segments={}",
                    out.total_cycles, out.user_cycles, out.segments
                );
            }
            Ok(())
        }
        RunOutcome::Rejected(reason) => {
            // A rejected receipt is a security-relevant failure: report it
            // honestly and exit non-zero. Nothing is returned as "verified".
            bail!("the receipt was NOT accepted as verified:\n  {reason}");
        }
    }
}

/// Select the (ELF, image ID, label) for the requested guest program.
fn select_guest(which: &str) -> Result<(&'static [u8], [u32; 8], &'static str)> {
    match which {
        "median" => Ok((MEDIAN_GUEST_ELF, MEDIAN_GUEST_ID, "MEDIAN_GUEST_ID")),
        "decoy" => Ok((DECOY_GUEST_ELF, DECOY_GUEST_ID, "DECOY_GUEST_ID")),
        other => bail!("unknown guest `{other}` (expected `median` or `decoy`)"),
    }
}

fn cmd_demo(args: &Args) -> Result<()> {
    let (elf, image_id, id_name) = select_guest(&args.guest)?;
    println!(
        "running guest: {} ({id_name} = {})",
        args.guest,
        image_id_hex(image_id)
    );
    println!("real cryptography (dev_mode={})", args.dev_mode);
    let input = read_input(&args.input)?;
    let outcome = run_pipeline(
        elf,
        image_id,
        &input.values,
        args.dev_mode,
        Some(&args.receipt),
        "demo",
    )?;
    report_outcome(outcome, args.json)
}

fn cmd_prove(args: &Args) -> Result<()> {
    let (elf, image_id, _) = select_guest(&args.guest)?;
    let input = read_input(&args.input)?;
    let outcome = run_pipeline(
        elf,
        image_id,
        &input.values,
        args.dev_mode,
        Some(&args.receipt),
        "prove",
    )?;
    report_outcome(outcome, args.json)
}

fn cmd_execute(args: &Args) -> Result<()> {
    let input = read_input(&args.input)?;
    validate_batch(&input.values).map_err(|e| anyhow::anyhow!("invalid batch: {e:?}"))?;
    let env = ExecutorEnv::builder().write(&input.values)?.build()?;
    let t = std::time::Instant::now();
    let session = default_executor()
        .execute(env, MEDIAN_GUEST_ELF)
        .context("execution FAILED")?;
    let ms = t.elapsed().as_millis();
    println!("execute only: {ms} ms");
    println!("exit code  : {:?}", session.exit_code);
    println!("segments   : {}", session.segments.len());
    println!("user cycles: {}", session.cycles());
    Ok(())
}

fn cmd_verify(args: &Args) -> Result<()> {
    let bytes = std::fs::read(&args.receipt)
        .with_context(|| format!("failed to read receipt `{}`", args.receipt))?;
    let mut receipt: risc0_zkvm::Receipt =
        bincode::deserialize(&bytes).context("failed to deserialize receipt")?;

    // Which program do we expect? The host must pin the exact image ID.
    let (expected_id, expected_name) = if args.expect_decoy {
        (DECOY_GUEST_ID, "DECOY_GUEST_ID")
    } else {
        (MEDIAN_GUEST_ID, "MEDIAN_GUEST_ID")
    };
    println!(
        "expecting program: {expected_name} = {}",
        image_id_hex(expected_id)
    );
    println!("receipt seal type: {}", verify::receipt_kind_name(&receipt));

    // Optional adversarial mutation of the public journal.
    if args.tamper_journal {
        let last = receipt
            .journal
            .bytes
            .last_mut()
            .context("cannot tamper an empty journal")?;
        *last ^= 0x01;
        println!("(tampered the final journal byte)");
    }

    // The batch the verifier believes was the program input.
    let bind_path = args.bind_input.as_deref().unwrap_or(&args.input);
    let input = read_input(bind_path)?;

    // Reuse the strict checker (fake rejection + dev-mode-off + image ID +
    // binding). Note: --expect-decoy with a median receipt (or vice versa)
    // fails at image-ID verification.
    let t = std::time::Instant::now();
    match verify::verify_strict(&receipt, expected_id, &input.values) {
        Ok(journal) => {
            let ms = t.elapsed().as_millis();
            println!("verify: {ms} ms -> OK");
            println!("length = {}, median = {}", journal.length, journal.median);
            println!(
                "input commitment = {}",
                hex::encode(journal.input_commitment)
            );
            Ok(())
        }
        Err(err) => bail!("verification FAILED:\n  {err}"),
    }
}

// Keep the decoy ELF referenced so its image ID/ELF are always generated even
// if a future refactor stops using the ELF in this binary.
#[allow(dead_code)]
const _DECOY_ELF_KEEP: &[u8] = DECOY_GUEST_ELF;
