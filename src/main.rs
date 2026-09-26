//! `ecstripe` binary: a JSON-driven front-end for the library.
//!
//! It reads one JSON request document, performs one operation and prints one
//! JSON response document. It contains no coding logic — see
//! [`ecstripe::cli::dispatch`].

mod cli;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let response = cli::dispatch(&args);
    let json = serde_json::to_string_pretty(&response).unwrap_or_else(|e| {
        format!("{{\"status\":\"error\",\"error\":{{\"kind\":\"internal\",\"message\":\"{e}\"}}}}")
    });
    println!("{json}");

    if matches!(response, cli::Response::Error { .. }) {
        std::process::exit(1);
    }
}
