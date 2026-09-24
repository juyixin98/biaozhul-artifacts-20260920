use std::sync::Arc;

use watchdog_host::clock::{Clock, ManualClock, SystemClock};
use watchdog_host::db;
use watchdog_host::http::{build_router, AppState};
use watchdog_host::service::WatchdogService;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let db_path = std::env::var("WATCHDOG_DB").unwrap_or_else(|_| "watchdog.db".into());
    let addr = std::env::var("WATCHDOG_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".into());
    let manual_clock = std::env::var("WATCHDOG_MANUAL_CLOCK").as_deref() == Ok("1");

    let conn = db::open(&db_path)?;
    let (clock, manual_handle) = if manual_clock {
        let start = SystemClock.now_ms();
        tracing::warn!(
            "manual clock ENABLED at {start} ms; /admin/clock endpoints available (testing only)"
        );
        let mc = Arc::new(ManualClock::new(start));
        (mc.clone() as Arc<dyn Clock>, Some(mc))
    } else {
        (Arc::new(SystemClock) as Arc<dyn Clock>, None)
    };

    let svc = Arc::new(WatchdogService::new(conn, clock));
    let app = build_router(AppState {
        svc,
        manual_clock: manual_handle,
    });

    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!("watchdog-host listening on http://{addr} (db={db_path})");
    axum::serve(listener, app).await?;
    Ok(())
}
