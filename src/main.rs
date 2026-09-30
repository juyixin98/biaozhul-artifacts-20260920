//! CLI entry: run the Axum validation server.
//!
//! Examples:
//!   iejoin serve --listen 127.0.0.1:8080
//!   iejoin serve --listen 127.0.0.1:8080 --max-rows 4096 --default-limit 128

use std::sync::Arc;

use clap::{Parser, Subcommand};
use tokio::net::TcpListener;
use tracing::info;

use iejoin_backend::http::{app, AppState};
use iejoin_backend::resource::ResourceBudget;
use iejoin_backend::state::RunRegistry;

#[derive(Parser, Debug)]
#[command(name = "iejoin", version, about = "IEJoin-style range join backend")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Run the HTTP validation server.
    Serve {
        #[arg(long, default_value = "127.0.0.1:8080")]
        listen: String,
        #[arg(long, default_value_t = 1_000_000)]
        max_rows: usize,
        #[arg(long, default_value_t = 1 << 18)]
        max_candidate_words: usize,
        #[arg(long, default_value_t = 1 << 22)]
        max_materialized_pairs: usize,
        #[arg(long, default_value_t = 1024)]
        default_limit: usize,
        #[arg(long, default_value_t = 1 << 20)]
        max_limit: usize,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let cli = Cli::parse();
    match cli.command {
        Command::Serve {
            listen,
            max_rows,
            max_candidate_words,
            max_materialized_pairs,
            default_limit,
            max_limit,
        } => {
            let budget = ResourceBudget {
                max_rows_per_side: max_rows,
                max_candidate_words,
                max_materialized_pairs,
                default_output_limit: default_limit,
                max_output_limit: max_limit,
            };
            let state = AppState {
                registry: Arc::new(RunRegistry::new()),
                budget,
            };
            let listener = TcpListener::bind(&listen).await?;
            info!(%listen, ?budget, "iejoin validation server listening");
            axum::serve(listener, app(state).into_make_service()).await?;
        }
    }
    Ok(())
}
