//! Watchdog host service entry point.
//!
//! Pure backend: exposes the recovery state machine over HTTP and persists
//! all state to SQLite. No hardware is touched.

use clap::Parser;
use std::sync::Arc;
use std::time::Duration;
use watchdog_host::{
    api, App, ClockMode, Config, ManualClock, SystemClock,
};

/// Host-side watchdog recovery state machine.
#[derive(Parser, Debug)]
#[command(name = "watchdog-host", version, about)]
struct Args {
    /// Bind address for the HTTP API.
    #[arg(long, default_value = "127.0.0.1:8080")]
    bind: String,

    /// SQLite database file (created if absent).
    #[arg(long, default_value = "watchdog.db")]
    db: String,

    /// Comma-separated names of the supervised critical tasks.
    #[arg(long, value_delimiter = ',', default_value = "control-loop,sensor-fusion,logger")]
    tasks: Vec<String>,

    /// Supervision window in milliseconds.
    #[arg(long, default_value_t = 5000)]
    window_ms: u64,

    /// Consecutive resets that trigger safe mode.
    #[arg(long, default_value_t = 3)]
    threshold: u32,

    /// Clock source: 'system' (wall clock, auto supervision) or 'manual'
    /// (time only moves via /clock/* — used for deterministic demos/tests).
    #[arg(long, default_value = "system")]
    clock: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let args = Args::parse();

    let config = Config {
        tasks: args
            .tasks
            .iter()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
        window_ms: args.window_ms,
        reset_threshold: args.threshold,
    };
    if config.tasks.is_empty() {
        anyhow_exit("at least one supervised task is required");
    }

    let (clock_mode, clock, manual_clock): (
        ClockMode,
        Arc<dyn watchdog_host::Clock>,
        Option<Arc<ManualClock>>,
    ) = match args.clock.as_str() {
        "system" => (
            ClockMode::System,
            Arc::new(SystemClock),
            None,
        ),
        "manual" => {
            let mc = Arc::new(ManualClock::new(0));
            (ClockMode::Manual, mc.clone(), Some(mc))
        }
        other => anyhow_exit(&format!("invalid --clock value '{other}' (use system|manual)")),
    };

    let app = App::open(&args.db, config, clock, manual_clock, clock_mode)?;

    // In system-clock mode run supervision automatically. In manual mode the
    // caller drives time with /clock/* and supervision with /tick.
    if clock_mode == ClockMode::System {
        let ticker_app = app.clone();
        let interval_ms = (args.window_ms / 4).clamp(50, 1000);
        tokio::spawn(async move {
            let mut timer = tokio::time::interval(Duration::from_millis(interval_ms));
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                timer.tick().await;
                let outcome = ticker_app.watchdog.lock().unwrap().tick();
                if let watchdog_host::core::TickOutcome::Reset {
                    reason,
                    safe_mode,
                    fault_generation,
                    ..
                } = outcome
                {
                    eprintln!(
                        "[supervisor] reset recorded (safe_mode={safe_mode}, gen={fault_generation}): {reason}"
                    );
                }
            }
        });
    }

    let listener = tokio::net::TcpListener::bind(&args.bind).await?;
    eprintln!(
        "watchdog-host listening on http://{} (clock={:?}, db={}, window={}ms, threshold={})",
        args.bind, clock_mode, args.db, args.window_ms, args.threshold
    );
    axum::serve(listener, api::router(app)).await?;
    Ok(())
}

fn anyhow_exit(msg: &str) -> ! {
    eprintln!("error: {msg}");
    std::process::exit(2);
}
