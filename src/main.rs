//! Command-line entry point.
//!
//! ```text
//! extsort serve <listen-addr> <repo-dir>
//!     Start the HTTP validation server (default 127.0.0.1:8080 / ./repo).
//!
//! extsort verify <input-file> <output-file> [key-spec]
//!     Independently validate an external-sort result by sorting the input
//!     fully in memory with Rust's stable slice sort and comparing byte by
//!     byte. Exits 0 on match, 1 on mismatch.
//!
//! extsort verify-run <run-file>
//!     Verify a single on-disk segment (magic, per-frame CRC, aggregate CRC,
//!     trailer). Exits 0 if intact, 1 if corrupt.
//!
//! extsort --help
//! ```

use std::fs::File;
use std::io::{BufReader, Read};
use std::path::PathBuf;
use std::process::ExitCode;

use extsort::format::Record;
use extsort::key::KeySpec;
use extsort::server::Server;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let prog = args.first().map(String::as_str).unwrap_or("extsort");

    match args.get(1).map(String::as_str) {
        Some("serve") => cmd_serve(&args[2..]),
        Some("verify") => cmd_verify(&args[2..]),
        Some("verify-run") => cmd_verify_run(&args[2..]),
        Some("-h") | Some("--help") | Some("help") | None => {
            print_usage(prog);
            ExitCode::SUCCESS
        }
        Some(other) => {
            eprintln!("unknown command: {other}\n");
            print_usage(prog);
            ExitCode::from(2)
        }
    }
}

fn print_usage(prog: &str) {
    eprint!(
        "{p} serve <listen-addr> <repo-dir>\n\
         {p} verify <input-file> <output-file> [key-spec]\n\
         {p} verify-run <run-file>\n\
         \n\
         key-spec examples: \"\" (whole line), \"1:asc\", \"2:desc;1:asc\"\n",
        p = prog
    );
}

fn cmd_serve(args: &[String]) -> ExitCode {
    let addr = args.first().map(String::as_str).unwrap_or("127.0.0.1:8080");
    let root = args.get(1).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("repo"));
    match std::fs::create_dir_all(&root) {
        Ok(()) => {}
        Err(e) => {
            eprintln!("cannot create repo {root:?}: {e}");
            return ExitCode::FAILURE;
        }
    }
    let server = match Server::bind(addr, &root) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("bind {addr} failed: {e}");
            return ExitCode::FAILURE;
        }
    };
    match server.run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("server error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn read_all_lines(path: &str) -> std::io::Result<Vec<Vec<u8>>> {
    let f = File::open(path)?;
    let mut reader = BufReader::new(f);
    let mut data = Vec::new();
    reader.read_to_end(&mut data)?;
    let mut lines = Vec::new();
    let mut start = 0;
    for (i, &b) in data.iter().enumerate() {
        if b == b'\n' {
            let mut end = i;
            if end > start && data[end - 1] == b'\r' {
                end -= 1;
            }
            lines.push(data[start..end].to_vec());
            start = i + 1;
        }
    }
    if start < data.len() {
        lines.push(data[start..].to_vec());
    }
    Ok(lines)
}

fn cmd_verify_run(args: &[String]) -> ExitCode {
    let path = match args.first() {
        Some(p) => p,
        None => {
            eprintln!("verify-run requires <run-file>");
            return ExitCode::from(2);
        }
    };
    // A run file lives under a repository root; open the file directly by
    // anchoring a RealFs at `/`.
    let abs = match std::fs::canonicalize(path) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("cannot open {path}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let fs = extsort::fs::RealFs::new(std::path::PathBuf::from("/"));
    let rel = abs.to_string_lossy().trim_start_matches('/').to_owned();
    match extsort::format::verify_run(&fs, &rel, 4096) {
        Ok(info) => {
            println!("OK: {} records, aggr_crc={:08x}", info.count, info.aggr_crc);
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("CORRUPT: {e}");
            ExitCode::FAILURE
        }
    }
}

fn cmd_verify(args: &[String]) -> ExitCode {
    let (input, output, key) = match (args.first(), args.get(1)) {
        (Some(i), Some(o)) => (i.as_str(), o.as_str(), args.get(2).map(String::as_str).unwrap_or("")),
        _ => {
            eprintln!("verify requires <input-file> <output-file> [key-spec]");
            return ExitCode::from(2);
        }
    };

    let spec = match KeySpec::parse(key) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("bad key spec: {e}");
            return ExitCode::from(2);
        }
    };

    let input_lines = match read_all_lines(input) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("cannot read {input}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let output_lines = match read_all_lines(output) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("cannot read {output}: {e}");
            return ExitCode::FAILURE;
        }
    };

    // Reference: stable sort fully in memory.
    let mut reference: Vec<Record> = input_lines
        .into_iter()
        .enumerate()
        .map(|(seq, line)| Record { seq: seq as u64, line })
        .collect();
    reference.sort_by(|a, b| spec.cmp(a, b));

    let n = reference.len();
    if output_lines.len() != n {
        eprintln!(
            "FAIL: record count differs: external={} reference={n}",
            output_lines.len()
        );
        return ExitCode::FAILURE;
    }
    for (i, (got, want)) in output_lines.iter().zip(reference.iter()).enumerate() {
        if got != &want.line {
            eprintln!("FAIL: first mismatch at record {i}");
            eprintln!("  external: {}", String::from_utf8_lossy(got));
            eprintln!("  reference: {}", String::from_utf8_lossy(&want.line));
            return ExitCode::FAILURE;
        }
    }
    println!("OK: {n} records match in-memory stable reference (key: {})", spec.canonical());
    ExitCode::SUCCESS
}
