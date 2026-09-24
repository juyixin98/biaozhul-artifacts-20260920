//! HTTP server entrypoint for the OCI layered whitelist unpacker.
//!
//! Usage:
//!   oci-unpack-server [--address 127.0.0.1] [--port 8080]
//!                     [--workdir ./workdir]
//!
//! Every option also reads its corresponding environment variable
//! (`OCI_ADDRESS`, `OCI_PORT`, `OCI_WORKDIR`); the flag wins.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use oci_unpack::{server, Limits, Workdir};

#[derive(Debug)]
struct Args {
    address: String,
    port: u16,
    workdir: PathBuf,
}

fn print_help() {
    println!(
        "oci-unpack-server — offline OCI layered whitelist extraction backend\n\
\n\
USAGE:\n    oci-unpack-server [OPTIONS]\n\
\n\
OPTIONS:\n\
    -a, --address <ADDR>   Bind address (env OCI_ADDRESS) [default: 127.0.0.1]\n\
    -p, --port <PORT>      Bind port    (env OCI_PORT)    [default: 8080]\n\
    -w, --workdir <DIR>    Working dir  (env OCI_WORKDIR) [default: ./workdir]\n\
    -h, --help             Print this help\n"
    );
}

fn parse_args() -> Result<Args, String> {
    let mut args = std::env::args().skip(1);
    let mut address = std::env::var("OCI_ADDRESS").unwrap_or_else(|_| "127.0.0.1".into());
    let mut port: u16 = std::env::var("OCI_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8080);
    let mut workdir = std::env::var("OCI_WORKDIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("./workdir"));

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            "-a" | "--address" => {
                address = args
                    .next()
                    .ok_or_else(|| format!("{arg} requires a value"))?;
            }
            "-p" | "--port" => {
                port = args
                    .next()
                    .ok_or_else(|| format!("{arg} requires a value"))?
                    .parse()
                    .map_err(|_| "invalid port".to_string())?;
            }
            "-w" | "--workdir" => {
                workdir = args
                    .next()
                    .map(PathBuf::from)
                    .ok_or_else(|| format!("{arg} requires a value"))?;
            }
            other => return Err(format!("unknown argument: {other} (see --help)")),
        }
    }
    Ok(Args {
        address,
        port,
        workdir,
    })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(2);
        }
    };

    // Make sure the workdir layout exists.
    let workdir = Arc::new(Workdir::new(&args.workdir));
    for sub in [
        workdir.images_dir(),
        workdir.staging_dir(),
        workdir.roots_dir(),
    ] {
        std::fs::create_dir_all(&sub)
            .map_err(|e| format!("cannot create {}: {e}", sub.display()))?;
    }

    let limits = Arc::new(Limits::default());
    let app = server::router(workdir.clone(), limits);

    let addr: SocketAddr = format!("{}:{}", args.address, args.port)
        .parse()
        .map_err(|_| format!("invalid bind address: {}:{}", args.address, args.port))?;
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| format!("failed to bind {addr}: {e}"))?;

    tracing::info!(%addr, workdir = %workdir.images_dir().parent().unwrap_or(std::path::Path::new(".")).display(),
        "OCI layered whitelist unpacker listening");
    eprintln!("listening on http://{addr}");
    eprintln!(
        "  fixtures: {}/images/<name>",
        workdir
            .images_dir()
            .parent()
            .map(|p| p.display().to_string())
            .unwrap_or_default()
    );
    eprintln!("  routes:   GET /health | GET /images | POST /rebuild/{{image}}");
    eprintln!("            GET /rebuild/{{image}} | .../file?path= | .../layers");

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutdown signal received");
        })
        .await?;
    Ok(())
}
