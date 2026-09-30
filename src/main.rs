//! CLI entry point. Subcommands:
//!
//! * `serve`  — run the Axum HTTP service (loads the configured CSV fixture);
//! * `verify` — run the independent differential verification and print a JSON
//!   report (exits non-zero if any case fails);
//! * `query`  — evaluate one predicate against the fixture table from the
//!   shell and print the three-valued result.

use std::collections::HashSet;
use std::sync::Arc;

use clap::{Parser, Subcommand};
use tracing::info;
use tracing_subscriber::{fmt, EnvFilter};

use tritmap::batch::LogicalType;
use tritmap::config::Config;
use tritmap::dataset::Dataset;
use tritmap::loader::{load_csv, CsvColumn};
use tritmap::query::parse_predicate;
use tritmap::server::AppState;

#[derive(Parser)]
#[command(name = "tritmap", version, about)]
struct Cli {
    /// Path to a TOML config file (defaults are used if omitted).
    #[arg(long, global = true)]
    config: Option<String>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Start the HTTP server.
    Serve,
    /// Run differential verification (bitmap engine vs scalar oracle).
    Verify {
        #[arg(long)]
        seed: Option<u64>,
        #[arg(long)]
        iterations: Option<usize>,
        #[arg(long)]
        rows: Option<usize>,
        #[arg(long = "null-density")]
        null_density: Option<u32>,
    },
    /// Evaluate one expression against the fixture table.
    Query {
        /// Predicate text, e.g. "age >= 18 AND NOT city IS NULL".
        #[arg(long)]
        expr: String,
        /// Optional pinned table version.
        #[arg(long)]
        version: Option<u64>,
        /// Row to delete before querying (repeatable), deletes happen at
        /// successive versions starting at data_version+1.
        #[arg(long = "delete")]
        delete: Vec<usize>,
    },
}

fn init_tracing() {
    let filter = EnvFilter::try_from_env("TRITMAP_LOG")
        .unwrap_or_else(|_| EnvFilter::new("info"));
    fmt()
        .with_env_filter(filter)
        .with_target(false)
        .compact()
        .init();
}

/// Fixture schema used by the shipped synthetic data.
fn fixture_schema() -> Vec<CsvColumn> {
    vec![
        CsvColumn::new("id", LogicalType::Integer),
        CsvColumn::new("age", LogicalType::Integer),
        CsvColumn::new("city", LogicalType::Text),
        CsvColumn::new("vip", LogicalType::Boolean),
    ]
}

fn load_fixture_table(cfg: &Config) -> tritmap::Result<Dataset> {
    let path = std::path::Path::new(&cfg.data.fixture_dir).join(&cfg.data.table_file);
    info!(path = %path.display(), version = cfg.data.data_version, "loading synthetic fixture");
    let batch = load_csv(&path, &fixture_schema())?;
    Dataset::load("users", batch, cfg.data.data_version, &[])
}

#[tokio::main]
async fn main() -> tritmap::Result<()> {
    init_tracing();
    let cli = Cli::parse();
    let cfg = Config::load(cli.config.as_deref())?;

    match cli.command {
        Command::Serve => {
            let state = Arc::new(AppState::new(cfg.clone()));
            let dataset = load_fixture_table(&cfg)?;
            state.insert_table("users", dataset);

            let addr = format!("{}:{}", cfg.server.host, cfg.server.port);
            let listener = tokio::net::TcpListener::bind(&addr).await?;
            info!(%addr, "server starting");
            tritmap::server::serve(listener, state).await?;
        }
        Command::Verify {
            seed,
            iterations,
            rows,
            null_density,
        } => {
            let state = Arc::new(AppState::new(cfg));
            let reports = state.run_differential_verify(
                seed.unwrap_or(state.config().verify.seed),
                iterations.unwrap_or(state.config().verify.iterations),
                rows.unwrap_or(state.config().verify.rows),
                null_density.unwrap_or(state.config().verify.null_density),
            )?;
            let all_passed = reports.iter().all(|r| r.passed);
            println!("{}", serde_json::to_string_pretty(&reports).unwrap());
            if !all_passed {
                std::process::exit(1);
            }
        }
        Command::Query {
            expr,
            version,
            delete,
        } => {
            let mut dataset = load_fixture_table(&cfg)?;
            let mut v = cfg.data.data_version;
            let mut deletions: Vec<(usize, u64)> = Vec::new();
            for row in delete {
                v += 1;
                dataset.delete_row(row, v)?;
                deletions.push((row, v));
            }
            let parsed = parse_predicate(&expr)?;
            // Resolve the effective query version so the oracle's dead set
            // matches the domain the engine actually evaluates against: a
            // deletion is only visible at versions >= its tombstone version.
            let query_version = version.unwrap_or_else(|| dataset.current_version());
            let dead: HashSet<usize> = deletions
                .iter()
                .filter(|(_, deleted_at)| *deleted_at <= query_version)
                .map(|(row, _)| *row)
                .collect();
            let outcome = tritmap::query::evaluate(&dataset, &parsed, Some(query_version))?;

            // Cross-check the ad-hoc query against the independent oracle too.
            let report = tritmap::verify::cross_check(
                &dataset,
                &parsed,
                Some(query_version),
                &dead,
                "cli-query",
                "fixture-query",
                0,
            )?;
            if !report.passed {
                eprintln!("ORACLE CROSS-CHECK FAILED: {report:?}");
                std::process::exit(2);
            }

            let rows: Vec<_> = outcome
                .partition
                .to_row_values()
                .into_iter()
                .enumerate()
                .map(|(row, t)| serde_json::json!({
                    "row": row,
                    "verdict": t.map(|v| v.as_str()),
                }))
                .collect();
            println!(
                "{}",
                serde_json::json!({
                    "ok": true,
                    "expr": expr,
                    "version": outcome.version,
                    "row_space": outcome.n,
                    "live_rows": outcome.live,
                    "true_rows": outcome.true_count(),
                    "false_rows": outcome.false_count(),
                    "unknown_rows": outcome.unknown_count(),
                    "deleted_rows": outcome.deleted_count(),
                    "selected_rows": outcome.selected_rows(),
                    "rows": rows,
                })
            );
        }
    }
    Ok(())
}
