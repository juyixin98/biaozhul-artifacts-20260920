//! Crash-injection helper for integration tests.
//!
//! Opens (or creates) an index, installs a hook that hard-exits the process
//! (`_exit(9)`) the first time the engine reaches a named durability point,
//! and performs exactly one mutating command. A parent test then reopens the
//! file in a separate process, which triggers deterministic intent replay.
//!
//! Usage:
//!   crash-runner --file <P> --op put|delete --key <K> [--value <V>] \
//!       --crash <POINT> [--capacity N] [--hash fnv1a64|constant|u64lowbits]
//!
//! Points: split.after_reservation | split.after_intent |
//!         split.after_buckets | split.after_directory |
//!         split.after_commit_meta |
//!         merge.after_reservation | merge.after_intent |
//!         merge.after_bucket | merge.after_directory |
//!         merge.after_commit_meta |
//!         shrink.after_intent
//!
//! Exit codes: 0 normal completion, 9 simulated crash, other = error.
use ext_hash_index::{Config, HashKind, Index};
use std::path::PathBuf;
use std::process::exit;
use std::sync::Arc;

struct Args {
    file: PathBuf,
    op: String,
    key: String,
    value: String,
    crash: String,
    capacity: usize,
    max_depth: u32,
    hash: HashKind,
}

fn die(msg: impl AsRef<str>) -> ! {
    eprintln!("crash-runner: {}", msg.as_ref());
    exit(2);
}

fn parse_args() -> Args {
    let mut a = Args {
        file: PathBuf::from("index.db"),
        op: "put".into(),
        key: String::new(),
        value: String::new(),
        crash: String::new(),
        capacity: 4,
        max_depth: 20,
        hash: HashKind::U64LowBits,
    };
    let mut it = std::env::args().skip(1);
    while let Some(f) = it.next() {
        let val = |it: &mut dyn Iterator<Item = String>| {
            it.next()
                .unwrap_or_else(|| die(format!("flag {} needs a value", f)))
        };
        match f.as_str() {
            "--file" => a.file = PathBuf::from(val(&mut it)),
            "--op" => a.op = val(&mut it),
            "--key" => a.key = val(&mut it),
            "--value" => a.value = val(&mut it),
            "--crash" => a.crash = val(&mut it),
            "--capacity" => {
                let v = val(&mut it);
                a.capacity = v.parse().unwrap_or_else(|_| die("bad capacity"));
            }
            "--max-depth" => {
                let v = val(&mut it);
                a.max_depth = v.parse().unwrap_or_else(|_| die("bad max-depth"));
            }
            "--hash" => {
                let v = val(&mut it);
                a.hash = HashKind::parse(&v).unwrap_or_else(|| die("bad hash"));
            }
            other => die(format!("unknown arg {}", other)),
        }
    }
    if a.key.is_empty() {
        die("--key is required");
    }
    a
}

fn main() {
    let args = parse_args();
    let point = args.crash.clone();

    let mut idx = match Index::open(&args.file) {
        Ok(i) => i,
        Err(_) if !args.file.exists() => {
            let cfg = Config {
                bucket_capacity: args.capacity,
                max_depth: args.max_depth,
                hash: args.hash,
                key_max: 128,
                val_max: 256,
            };
            Index::create(&args.file, &cfg).unwrap_or_else(|e| die(e.to_string()))
        }
        Err(e) => die(format!("open failed: {}", e)),
    };

    if let Some(tag) = idx.recovered_intent() {
        eprintln!(
            "crash-runner: note: replay recovered a prior {} intent",
            tag
        );
    }

    idx.set_hook(Arc::new(move |p: &str| {
        if p == point {
            // Hard exit: no Drop, no flush — simulates power loss. The index
            // file is in exactly the state of the last fsync barrier.
            eprintln!("crash-runner: SIMULATED CRASH at {}", p);
            // SAFETY: immediate process termination by choice; no FFI invariants.
            unsafe {
                libc_exit(9);
            }
        }
    }));

    let key = args.key.clone();
    let res = match args.op.as_str() {
        "put" => idx.put(key.as_bytes(), args.value.as_bytes()),
        "delete" => idx.delete(key.as_bytes()),
        other => die(format!("unknown op {}", other)),
    };
    match res {
        Ok(_) => {
            eprintln!("crash-runner: {} {} completed", args.op, key);
            exit(0);
        }
        Err(e) => die(format!("op failed: {}", e)),
    }
}

// Use the raw exit syscall via std::process; `exit` from libc is mirrored by
// std::process::exit except it still runs atexit — Rust's does not flush Rust
// buffers beyond stdio, which is sufficient here (index durability relies on
// explicit fsync, not on process teardown).
unsafe fn libc_exit(code: i32) -> ! {
    // Avoid stdio buffering of the message above.
    let _ = std::io::Write::flush(&mut std::io::stderr());
    std::process::exit(code)
}
