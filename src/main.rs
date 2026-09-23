//! Command-line entry point:
//!
//! ```text
//! cas-repo serve --data <dir> [--addr 127.0.0.1:8080]
//! ```
//!
//! All in-flight requests finish naturally; Ctrl-C (SIGINT) terminates the
//! process. Root and block writes are atomic, so termination at any point
//! leaves a consistent on-disk repository.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use cas_repo::server::{Server, ServerConfig};
use cas_repo::store::DynStore;
use cas_repo::vfs::{StdVfs, Vfs};

fn print_usage() {
    eprintln!(
        "cas-repo — content-addressable block repository\n\
\n\
USAGE:\n    cas-repo serve --data <DATA_DIR> [--addr 127.0.0.1:8080]\n"
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut data: Option<PathBuf> = None;
    let mut addr = "127.0.0.1:8080".to_string();
    let mut iter = args.iter();
    while let Some(a) = iter.next() {
        match a.as_str() {
            "serve" => {}
            "--data" | "-d" => data = iter.next().map(PathBuf::from),
            "--addr" | "-a" => {
                if let Some(v) = iter.next() {
                    addr = v.clone();
                }
            }
            "-h" | "--help" => {
                print_usage();
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("unknown argument: {other}");
                print_usage();
                return ExitCode::FAILURE;
            }
        }
    }

    let data = match data {
        Some(d) => d,
        None => {
            eprintln!("missing --data <DATA_DIR>");
            print_usage();
            return ExitCode::FAILURE;
        }
    };

    let vfs: Box<dyn Vfs> = Box::new(StdVfs::new());
    let store: Arc<DynStore> = match DynStore::open_boxed(&data, vfs) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to open store at {}: {e}", data.display());
            return ExitCode::FAILURE;
        }
    };

    let server = match Server::bind(&addr, store) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("failed to bind {addr}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let bound = server.local_addr().unwrap();
    eprintln!(
        "cas-repo listening on http://{bound} (data: {})",
        data.display()
    );
    eprintln!("press Ctrl-C to stop");

    server.serve(ServerConfig::default());
    ExitCode::SUCCESS
}
