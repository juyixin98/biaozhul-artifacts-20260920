//! repro-pack command line: runs the HTTP server or performs one pack/verify.
//!
//! With no subcommand the server is started (equivalent to `serve`).

use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use repro_pack::{pack, server::serve, verify, PackOptions, VerifyOptions};

/// Deterministic artifact packaging service.
#[derive(Parser, Debug)]
#[command(name = "repro-pack", version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Start the HTTP server.
    Serve {
        /// Bind address (default 127.0.0.1:8080; env REPRO_PACK_BIND overrides).
        #[arg(long, value_name = "ADDR")]
        bind: Option<String>,
    },
    /// Package one directory and print the result as JSON.
    Pack {
        /// Source directory to package.
        #[arg(long)]
        source: PathBuf,
        /// Directory artifacts are written to.
        #[arg(long)]
        output_dir: PathBuf,
        /// Artifact base name (<name>.tar / <name>.manifest.json / <name>.sha256).
        #[arg(long, default_value = "artifact")]
        name: String,
        /// Fixed entry mtime in seconds since the Unix epoch (default 0).
        #[arg(long, default_value_t = 0)]
        mtime_epoch: i64,
        /// Overwrite existing artifacts.
        #[arg(long)]
        overwrite: bool,
    },
    /// Verify an artifact against its manifest (and optionally a source tree).
    Verify {
        /// Path to the .tar artifact.
        #[arg(long)]
        tar: PathBuf,
        /// Manifest path (default: sibling .manifest.json).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// Digest path (default: sibling .sha256).
        #[arg(long)]
        digest: Option<PathBuf>,
        /// Source directory for a full reproducibility rebuild.
        #[arg(long)]
        source: Option<PathBuf>,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command.unwrap_or(Command::Serve { bind: None }) {
        Command::Serve { bind } => {
            let addr_str = bind
                .or_else(|| std::env::var("REPRO_PACK_BIND").ok())
                .unwrap_or_else(|| "127.0.0.1:8080".to_string());
            let addr: SocketAddr = match addr_str.parse() {
                Ok(a) => a,
                Err(e) => {
                    eprintln!("invalid bind address {addr_str:?}: {e}");
                    return ExitCode::from(2);
                }
            };
            eprintln!("repro-pack listening on http://{addr}");
            match serve(addr).await {
                Ok(()) => ExitCode::SUCCESS,
                Err(e) => {
                    eprintln!("server error: {e}");
                    ExitCode::FAILURE
                }
            }
        }
        Command::Pack {
            source,
            output_dir,
            name,
            mtime_epoch,
            overwrite,
        } => {
            let mut opts = PackOptions::new(source, output_dir, name);
            opts.mtime_epoch = mtime_epoch;
            opts.overwrite = overwrite;
            match pack(&opts) {
                Ok(out) => {
                    let json = serde_json::json!({
                        "ok": true,
                        "tar_path": out.tar_path.display().to_string(),
                        "manifest_path": out.manifest_path.display().to_string(),
                        "digest_path": out.digest_path.display().to_string(),
                        "sha256": out.sha256,
                        "archive_size": out.archive_size,
                        "entry_count": out.entry_count,
                    });
                    println!("{}", serde_json::to_string_pretty(&json).unwrap());
                    ExitCode::SUCCESS
                }
                Err(e) => fail(e),
            }
        }
        Command::Verify {
            tar,
            manifest,
            digest,
            source,
        } => {
            let mut opts = VerifyOptions::new(tar);
            opts.manifest_path = manifest;
            opts.digest_path = digest;
            opts.source = source;
            match verify(&opts) {
                Ok(out) => {
                    let json = serde_json::json!({
                        "ok": out.ok,
                        "sha256": out.sha256,
                        "archive_size": out.archive_size,
                        "entry_count": out.entry_count,
                        "digest_matches": out.digest_matches,
                        "manifest_matches": out.manifest_matches,
                        "rebuild_matches": out.rebuild_matches,
                        "problems": out.problems,
                    });
                    println!("{}", serde_json::to_string_pretty(&json).unwrap());
                    if out.ok {
                        ExitCode::SUCCESS
                    } else {
                        ExitCode::from(3)
                    }
                }
                Err(e) => fail(e),
            }
        }
    }
}

fn fail(e: repro_pack::Error) -> ExitCode {
    eprintln!("error: {e}");
    ExitCode::FAILURE
}
