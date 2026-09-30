//! Command-line entry points:
//! * `bitindex3vl serve  [--config PATH]` — start the HTTP validation entry;
//! * `bitindex3vl demo   [--config PATH]` — build the synthetic fixture in
//!   memory, run a set of representative queries and print the run ids,
//!   T/F/U row sets and evaluation steps (offline, no network).

use std::path::PathBuf;
use std::process::ExitCode;

use bitindex3vl::config::Config;
use bitindex3vl::engine::Engine;
use bitindex3vl::error::IndexError;
use bitindex3vl::eval::Strategy;
use bitindex3vl::fixtures;
use bitindex3vl::predicate::Predicate;
use bitindex3vl::scalar::{CompareOp, Scalar};
use serde_json::json;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let command = args.get(1).map(String::as_str).unwrap_or("help");
    let config_path = config_path_from_args(&args);
    let config = match Config::load(config_path.as_deref()) {
        Ok(c) => c,
        Err(e) => return fail(e),
    };
    init_tracing(&config.log_filter);

    match command {
        "serve" => {
            let engine = Engine::with_limits(config.max_rows_per_table);
            let rt = match tokio::runtime::Builder::new_multi_thread().enable_all().build() {
                Ok(rt) => rt,
                Err(e) => return fail(io_error("failed to start the async runtime", e)),
            };
            if let Err(e) = rt.block_on(bitindex3vl::server::serve(&config, engine)) {
                return fail(io_error("HTTP server terminated with an error", e));
            }
            ExitCode::SUCCESS
        }
        "demo" => {
            let mut engine = Engine::with_limits(config.max_rows_per_table);
            if let Err(e) = run_demo(&mut engine) {
                return fail(e);
            }
            ExitCode::SUCCESS
        }
        "help" | "--help" | "-h" => {
            println!("usage: bitindex3vl <serve|demo|help> [--config PATH]");
            ExitCode::SUCCESS
        }
        other => {
            eprintln!("unknown command {other:?}; expected serve|demo|help");
            ExitCode::from(2)
        }
    }
}

fn init_tracing(filter: &str) {
    use tracing_subscriber::EnvFilter;
    let env_filter = EnvFilter::try_new(filter).unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_target(false)
        .try_init();
}

fn config_path_from_args(args: &[String]) -> Option<PathBuf> {
    let mut iter = args.iter().skip(2);
    while let Some(a) = iter.next() {
        if a == "--config" {
            if let Some(p) = iter.next() {
                return Some(PathBuf::from(p));
            }
        } else if let Some(rest) = a.strip_prefix("--config=") {
            return Some(PathBuf::from(rest));
        }
    }
    None
}

fn io_error(context: &str, e: std::io::Error) -> IndexError {
    IndexError::new(bitindex3vl::error::ErrorKind::InvalidConfig, format!("{context}: {e}"))
}

fn fail(e: IndexError) -> ExitCode {
    eprintln!("error: {e}");
    ExitCode::from(2)
}

fn run_demo(engine: &mut Engine) -> Result<(), IndexError> {
    fixtures::install_fixture(engine)?;
    let table = fixtures::FIXTURE_TABLE;

    struct Case {
        name: &'static str,
        pred: Predicate,
        strategy: Strategy,
        version: Option<u64>,
    }

    let cases = [
        Case {
            name: "composite equality conjunction (combined index)",
            pred: Predicate::and(vec![
                Predicate::compare("grp", CompareOp::Eq, Scalar::Int(2)),
                Predicate::compare("tag", CompareOp::Eq, Scalar::Text("t1".into())),
            ]),
            strategy: Strategy::Combined,
            version: Some(2),
        },
        Case {
            name: "NOT of a range predicate (NULL stays unknown)",
            pred: Predicate::not(Predicate::compare("score", CompareOp::Ge, Scalar::Float(4.0))),
            strategy: Strategy::Single,
            version: Some(2),
        },
        Case {
            name: "OR / AND / NOT / IS NULL combination at version 0",
            pred: Predicate::and(vec![
                Predicate::or(vec![
                    Predicate::compare("flag", CompareOp::Eq, Scalar::Bool(true)),
                    Predicate::is_null("flag"),
                ]),
                Predicate::not(Predicate::is_null("tag")),
            ]),
            strategy: Strategy::Auto,
            version: Some(0),
        },
        Case {
            name: "all-unknown comparison against the all-NULL column",
            pred: Predicate::compare("note", CompareOp::Eq, Scalar::Text("x".into())),
            strategy: Strategy::Auto,
            version: Some(2),
        },
    ];

    for case in cases {
        let outcome = engine.query(table, &case.pred, case.strategy, case.version)?;
        let rendered = json!({
            "case": case.name,
            "run_id": outcome.run_id,
            "table": outcome.table,
            "read_version": outcome.read_version,
            "strategy": outcome.strategy,
            "predicate": outcome.predicate,
            "counts": outcome.counts,
            "true_rows": outcome.true_rows,
            "false_rows": outcome.false_rows,
            "unknown_rows": outcome.unknown_rows,
            "steps": outcome.steps,
        });
        println!("{}", serde_json::to_string_pretty(&rendered).expect("demo outcome serializes"));
    }
    Ok(())
}
