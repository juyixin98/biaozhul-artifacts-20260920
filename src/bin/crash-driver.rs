//! `crash-driver`: tiny CLI over the same on-disk store as the HTTP server.
//!
//! Used to drive real-process crash recovery from shell scripts: arm a fault with
//! COW_CRASH_KILL=1, run an operation, the process exits 37 at the fault point, then
//! reopen (which runs recovery) and inspect the result.
//!
//! Usage: crash-driver --dir <DATA_DIR> <command> [args]
//!   init
//!   branch <name> [parent]
//!   write <branch> <index> <text-data>
//!   write-file <branch> <index> <path>
//!   read <branch> <index>           -> prints "<page_id> <base64>"
//!   delete <branch>
//!   dump                             -> JSON stats + every live page
//!   fault <point>                    -> arm crash point (e.g. before_manifest, after_page)
//!   fault-nth <point> <n>            -> arm the n-th hit of a point
//!   fault-clear
//!
//! Set COW_CRASH_KILL=1 so an armed fault terminates the process with exit code 37.

use std::process::ExitCode;

use cow_snapshot::base64;
use cow_snapshot::store::Store;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mut dir = "cow-data".to_string();
    let mut rest: Vec<String> = Vec::new();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--dir" => {
                i += 1;
                dir = args[i].clone();
            }
            other => rest.push(other.to_string()),
        }
        i += 1;
    }
    if rest.is_empty() {
        eprintln!("{}", USAGE);
        return ExitCode::from(2);
    }
    let cmd = rest[0].as_str();
    let a = &rest[1..];

    let run = || -> Result<(), String> {
        match cmd {
            "init" => {
                let _ = Store::open(&dir).map_err(|e| e.to_string())?;
                println!("initialized {dir}");
            }
            "branch" => {
                let (name, parent) = match a.len() {
                    1 => (a[0].clone(), None),
                    2 => (a[0].clone(), Some(a[1].clone())),
                    _ => return Err("branch <name> [parent]".into()),
                };
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                let out = s
                    .create_branch(&name, parent.as_deref())
                    .map_err(|e| e.to_string())?;
                println!("{}", serde_json::to_string(&out).unwrap());
            }
            "write" | "write-file" => {
                if a.len() != 3 {
                    return Err("write <branch> <index> <data|@file>".into());
                }
                let data = if cmd == "write-file" || a[2].starts_with('@') {
                    let path = a[2].trim_start_matches('@');
                    std::fs::read(path).map_err(|e| format!("read {path}: {e}"))?
                } else {
                    a[2].as_bytes().to_vec()
                };
                let index: usize = a[1].parse().map_err(|_| "bad index".to_string())?;
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                let out = s
                    .write_page(&a[0], index, &data)
                    .map_err(|e| e.to_string())?;
                println!("{}", serde_json::to_string(&out).unwrap());
            }
            "read" => {
                if a.len() != 2 {
                    return Err("read <branch> <index>".into());
                }
                let index: usize = a[1].parse().map_err(|_| "bad index".to_string())?;
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                match s.read_page(&a[0], index).map_err(|e| e.to_string())? {
                    None => println!("0 empty"),
                    Some(bytes) => {
                        let pid = s.page_id_at(&a[0], index).map_err(|e| e.to_string())?;
                        println!("{pid} {}", base64::encode(&bytes));
                    }
                }
            }
            "delete" => {
                if a.len() != 1 {
                    return Err("delete <branch>".into());
                }
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                s.delete_branch(&a[0]).map_err(|e| e.to_string())?;
                println!("deleted {}", a[0]);
            }
            "dump" => {
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                let st = s.stats().map_err(|e| e.to_string())?;
                println!("{}", serde_json::to_string_pretty(&st).unwrap());
            }
            "fault" => {
                if a.len() != 1 {
                    return Err("fault <point>".into());
                }
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                s.set_fault(Some(&a[0])).map_err(|e| e.to_string())?;
                println!("armed {}", a[0]);
            }
            "fault-nth" => {
                if a.len() != 2 {
                    return Err("fault-nth <point> <n>".into());
                }
                let n: u64 = a[1].parse().map_err(|_| "bad n".to_string())?;
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                let directive = format!("{}:{n}", a[0]);
                s.set_fault(Some(&directive)).map_err(|e| e.to_string())?;
                println!("armed {directive}");
            }
            "fault-clear" => {
                let s = Store::open(&dir).map_err(|e| e.to_string())?;
                s.set_fault(None).map_err(|e| e.to_string())?;
                println!("disarmed");
            }
            other => return Err(format!("unknown command {other}")),
        }
        Ok(())
    };

    if let Err(e) = run() {
        eprintln!("error: {e}");
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

const USAGE: &str = "usage: crash-driver --dir <DATA_DIR> <init|branch|write|write-file|read|delete|dump|fault|fault-nth|fault-clear> ...";
