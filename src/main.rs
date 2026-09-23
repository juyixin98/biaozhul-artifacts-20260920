//! tsblock command line entry point.
//!
//! Starts the local HTTP verification server.
//!
//! ```text
//! tsblock --data-dir ./data --addr 127.0.0.1:8080 [--block-size 1000] [--fault]
//! ```

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tsblock::server::{self, FaultMode};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut data_dir = PathBuf::from("tsblock-data");
    let mut addr = "127.0.0.1:0".to_string();
    let mut block_size: u32 = 1000;
    let mut fault = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--data-dir" | "-d" => {
                i += 1;
                data_dir = PathBuf::from(args.get(i).unwrap_or_else(|| usage_and_exit()));
            }
            "--addr" | "-a" => {
                i += 1;
                addr = args.get(i).cloned().unwrap_or_else(|| usage_and_exit());
            }
            "--block-size" | "-b" => {
                i += 1;
                block_size = args
                    .get(i)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or_else(|| usage_and_exit());
            }
            "--fault" => fault = true,
            "--help" | "-h" => {
                print_help();
                return;
            }
            other => {
                eprintln!("unknown argument: {other}");
                usage_and_exit();
            }
        }
        i += 1;
    }

    let mode = if fault { FaultMode::On } else { FaultMode::Off };

    let handle = match server::serve(&addr, data_dir.clone(), block_size.max(1), mode) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("failed to bind {addr}: {e}");
            std::process::exit(1);
        }
    };

    println!(
        "tsblock listening on http://{} (data_dir={}, block_size={}, fault_injection={})",
        handle.addr(),
        data_dir.display(),
        block_size,
        fault
    );

    install_signal_handler();

    // Block until SIGINT/SIGTERM; the server runs on background threads.
    while !SHUTDOWN.load(Ordering::SeqCst) {
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    handle.shutdown();
    // Give worker threads a moment to notice the stop flag.
    std::thread::sleep(std::time::Duration::from_millis(50));
    println!("shutting down");
}

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
fn install_signal_handler() {
    extern "C" fn handler(_: i32) {
        SHUTDOWN.store(true, Ordering::SeqCst);
    }
    unsafe {
        // SAFETY: handler only sets an atomic and is async-signal-safe.
        libc_signal(2, handler as extern "C" fn(i32) as usize); // SIGINT
        libc_signal(15, handler as extern "C" fn(i32) as usize); // SIGTERM
    }
}

#[cfg(windows)]
fn install_signal_handler() {
    // No signal handling on Windows; Ctrl+C terminates the process.
}

// Minimal direct FFI so the project stays dependency-free.
#[cfg(unix)]
unsafe extern "C" {
    fn signal(signum: i32, handler: usize) -> usize;
}
#[cfg(unix)]
unsafe fn libc_signal(signum: i32, handler: usize) -> usize {
    unsafe { signal(signum, handler) }
}

fn print_help() {
    println!(
        "tsblock — file-backed integer time-series block store\n\n\
USAGE:\n    tsblock [OPTIONS]\n\n\
OPTIONS:\n\
    -d, --data-dir <DIR>      storage directory (default ./tsblock-data)\n\
    -a, --addr <ADDR>         listen address (default 127.0.0.1:0, ephemeral)\n\
    -b, --block-size <N>      points per block (default 1000)\n\
        --fault               enable /dev/fault I/O injection endpoint\n\
    -h, --help                print this help\n"
    );
}

fn usage_and_exit() -> ! {
    eprintln!("try `tsblock --help`");
    std::process::exit(2);
}

// Keep the Arc import used on all platforms.
#[allow(dead_code)]
fn _assert_arc(_: Arc<()>) {}
