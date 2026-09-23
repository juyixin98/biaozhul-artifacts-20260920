//! Command-line entry point.
//!
//! Commands:
//! * `serve --root DIR [--addr 127.0.0.1:8080]`
//!   Run the local HTTP validation server.
//! * `sort --input FILE --output FILE [--root DIR] [--spec SPEC]
//!          [--delim ,] [--budget BYTES] [--lanes N] [--keep-temp]
//!          [--fault GRAMMAR]`
//!   Run one job end to end (resumable). With `--fault`, the named faults are
//!   injected; rerun with `--resume` to demonstrate recovery.
//! * `resume --root DIR --job ID`
//!   Reopen a crashed/interrupted job and continue.

use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

use extsort::io::{FaultPlan, FaultVfs, RealVfs};
use extsort::repository::{JobConfig, Repository};
use extsort::server::ServerConfig;
use extsort::{run_job, Error};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), Error> {
    let cmd = args.get(1).map(|s| s.as_str()).unwrap_or("help");
    match cmd {
        "serve" => cmd_serve(&args[2..]),
        "sort" => cmd_sort(&args[2..]),
        "resume" => cmd_resume(&args[2..]),
        "help" | "--help" | "-h" => {
            print_help();
            Ok(())
        }
        other => {
            eprintln!("unknown command: {other}\n");
            print_help();
            Err(Error::BadRequest("unknown command".into()))
        }
    }
}

fn print_help() {
    println!(
        "extsort — bounded-memory file-backed stable external sort\n\
\n\
USAGE:\n\
  extsort serve  --root DIR [--addr 127.0.0.1:8080]\n\
  extsort sort   --input FILE --output FILE [--root DIR]\n\
                 [--spec \"field:asc,field:desc\"] [--delim ,]\n\
                 [--budget BYTES] [--lanes N] [--keep-temp]\n\
                 [--fault GRAMMAR] [--job ID]\n\
  extsort resume --root DIR --job ID\n"
    );
}

struct Opts {
    map: Vec<(String, String)>,
}

fn parse_opts(args: &[String]) -> Result<Opts, Error> {
    // Options that are boolean switches and consume no following value.
    const FLAGS: &[&str] = &["keep-temp"];
    let mut map = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if let Some(key) = a.strip_prefix("--") {
            if let Some((k, v)) = key.split_once('=') {
                map.push((k.to_string(), v.to_string()));
            } else if FLAGS.contains(&key) {
                map.push((key.to_string(), "1".to_string()));
            } else if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                map.push((key.to_string(), args[i + 1].clone()));
                i += 1;
            } else if i + 1 < args.len() {
                // Value that happens to start with "--" (rare) still consumed.
                map.push((key.to_string(), args[i + 1].clone()));
                i += 1;
            } else {
                return Err(Error::BadRequest(format!("option {a} needs a value")));
            }
        }
        i += 1;
    }
    Ok(Opts { map })
}

impl Opts {
    fn get(&self, key: &str) -> Option<&str> {
        self.map
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }
}

fn cmd_serve(args: &[String]) -> Result<(), Error> {
    let opts = parse_opts(args)?;
    let root = opts.get("root").unwrap_or("./extsort-data").to_string();
    let addr = opts.get("addr").unwrap_or("127.0.0.1:8080").to_string();
    extsort::server::serve(ServerConfig { root, addr })
}

fn cmd_sort(args: &[String]) -> Result<(), Error> {
    let opts = parse_opts(args)?;
    let input = opts
        .get("input")
        .ok_or_else(|| Error::BadRequest("--input is required".into()))?;
    let output = opts
        .get("output")
        .ok_or_else(|| Error::BadRequest("--output is required".into()))?;
    let root = opts
        .get("root")
        .map(|s| s.to_string())
        .unwrap_or_else(temp_root);

    let cfg = JobConfig {
        key_spec: opts.get("spec").unwrap_or("").to_string(),
        delim: opts
            .get("delim")
            .map(|d| d.as_bytes().first().copied().unwrap_or(b','))
            .unwrap_or(b','),
        budget: opts
            .get("budget")
            .map(|b| b.parse::<u64>())
            .transpose()
            .map_err(|_| Error::BadRequest("bad --budget".into()))?
            .unwrap_or(4 * 1024 * 1024),
        max_lanes: opts
            .get("lanes")
            .map(|b| b.parse::<usize>())
            .transpose()
            .map_err(|_| Error::BadRequest("bad --lanes".into()))?
            .unwrap_or(4),
        keep_temp: opts.get("keep-temp").is_some(),
    };

    let data = fs::read(input).map_err(|e| Error::io(e, input.to_string()))?;
    let id = opts
        .get("job")
        .map(|s| s.to_string())
        .unwrap_or_else(|| "local".to_string());

    let repo = if let Some(grammar) = opts.get("fault") {
        let plan = FaultPlan::parse(grammar)
            .map_err(|m| Error::BadRequest(format!("bad --fault: {m}")))?;
        let real = Arc::new(RealVfs::new(PathBuf::from(&root)));
        Repository::with_vfs(&root, Arc::new(FaultVfs::new(real, plan)))
    } else {
        Repository::new(&root)
    };
    let vfs = repo.vfs();

    // Remove a stale same-id job so `sort` starts clean (resume uses `resume`).
    vfs.remove_dir_all(&format!("jobs/{id}"))?;
    let mut job = repo.create_job(&id, cfg.clone())?;
    extsort::io::atomic_write_bytes(
        vfs.as_ref(),
        &format!("jobs/{id}/input.dat.tmp"),
        &format!("jobs/{id}/input.dat"),
        &data,
    )?;

    let stats = run_job(&repo, &mut job)?;
    // Copy output to the requested destination.
    let produced = extsort::io::read_all(vfs.as_ref(), &format!("jobs/{id}/output.txt"))?;
    fs::write(output, produced).map_err(|e| Error::io(e, output.to_string()))?;

    eprintln!(
        "done: records={} level0_runs={} merge_passes={} merge_batches={} output_bytes={}",
        stats.records,
        stats.level0_runs,
        stats.merge_passes,
        stats.merge_batches,
        stats.output_bytes
    );
    eprintln!("job state kept at {root}/jobs/{id}");
    Ok(())
}

fn cmd_resume(args: &[String]) -> Result<(), Error> {
    let opts = parse_opts(args)?;
    let root = opts
        .get("root")
        .ok_or_else(|| Error::BadRequest("--root is required".into()))?;
    let id = opts
        .get("job")
        .ok_or_else(|| Error::BadRequest("--job is required".into()))?;
    let output = opts.get("output");

    let repo = Repository::new(root);
    let mut job = repo.open_job(id)?;
    let stats = run_job(&repo, &mut job)?;
    eprintln!(
        "resumed to done: records={} level0_runs={} merge_passes={} resumed=true output_bytes={}",
        stats.records, stats.level0_runs, stats.merge_passes, stats.output_bytes
    );
    if let Some(path) = output {
        let bytes = extsort::io::read_all(repo.vfs().as_ref(), &format!("jobs/{id}/output.txt"))?;
        fs::write(path, bytes).map_err(|e| Error::io(e, path.to_string()))?;
    }
    Ok(())
}

fn temp_root() -> String {
    let dir = std::env::temp_dir().join(format!(
        "extsort-cli-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    dir.display().to_string()
}
