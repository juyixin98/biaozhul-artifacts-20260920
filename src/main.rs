//! Build output-tree merge service — binary entrypoint.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;

use tokio::net::TcpListener;

use build_output_merge::api;

#[derive(Debug)]
struct Args {
    listen: SocketAddr,
    base_dir: PathBuf,
}

fn parse_args() -> Result<Args, String> {
    let mut listen: SocketAddr = "127.0.0.1:8080".parse().unwrap();
    let mut base_dir = PathBuf::from("var/merges");
    let mut iter = std::env::args().skip(1);
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--listen" | "-l" => {
                let v = iter
                    .next()
                    .ok_or_else(|| "missing value for --listen".to_string())?;
                listen = v
                    .parse()
                    .map_err(|e| format!("invalid --listen address {v:?}: {e}"))?;
            }
            "--base-dir" | "-d" => {
                let v = iter
                    .next()
                    .ok_or_else(|| "missing value for --base-dir".to_string())?;
                base_dir = PathBuf::from(v);
            }
            "--help" | "-h" => {
                print_usage();
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(Args { listen, base_dir })
}

fn print_usage() {
    println!(
        "build-output-merge\n\n\
USAGE:\n    build-output-merge [--listen 127.0.0.1:8080] [--base-dir var/merges]\n\n\
OPTIONS:\n\
    -l, --listen <ADDR>   HTTP listen address (default 127.0.0.1:8080)\n\
    -d, --base-dir <DIR>  Directory under which merged trees are published\n\
                          (default: var/merges)\n\
    -h, --help             Show this help\n"
    );
}

#[tokio::main]
async fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("argument error: {e}");
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    if let Err(e) = std::fs::create_dir_all(&args.base_dir) {
        eprintln!(
            "cannot create base directory {}: {e}",
            args.base_dir.display()
        );
        return ExitCode::FAILURE;
    }

    let listener = match TcpListener::bind(args.listen).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("cannot bind {}: {e}", args.listen);
            return ExitCode::FAILURE;
        }
    };
    println!(
        "build-output-merge listening on http://{} (base dir: {})",
        args.listen,
        args.base_dir.display()
    );

    let app = api::app(args.base_dir.clone());

    let serve = axum::serve(listener, app).with_graceful_shutdown(async {
        let _ = tokio::signal::ctrl_c().await;
        println!("\nshutdown signal received, draining");
    });

    if let Err(e) = serve.await {
        eprintln!("server error: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
