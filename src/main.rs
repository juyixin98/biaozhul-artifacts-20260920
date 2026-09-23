//! CLI: file-backed store + local HTTP validation server, plus offline
//! crash-injection demos.

use std::process::ExitCode;

use dual_sb_recovery::demo;
use dual_sb_recovery::io::RealVfs;
use dual_sb_recovery::repo::Repository;

const HELP: &str = "\
dual-sb — append-only log with dual alternating superblocks

USAGE:
  dual-sb serve --dir <DIR> [--addr 127.0.0.1:8080]
      Start the local HTTP validation server against a real on-disk store.

  dual-sb init   --dir <DIR>
      Create/open a repository and print recovery status.

  dual-sb put    --dir <DIR> --key <K> --value <V>
  dual-sb get    --dir <DIR> --key <K>
  dual-sb delete --dir <DIR> --key <K>
  dual-sb list   --dir <DIR>

  dual-sb demo matrix
      Crash during commit 3 at every commit point x every torn variant
      (8 x 3 = 24 cases), reboot the simulated media and recover.
  dual-sb demo matrix2
      Same, crashing during commit 2 (first rollback), half-page torn.
  dual-sb demo scenarios
      The three named acceptance scenarios: half-page superblock write,
      both roots corrupt, out-of-bounds newer root rollback.
  dual-sb demo crash --point <POINT> --torn <None|Half|Short> [--round N]
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let cmd = args.first().map(|s| s.as_str()).unwrap_or("help");
    match cmd {
        "help" | "--help" | "-h" => {
            print!("{HELP}");
            Ok(())
        }
        "serve" => {
            let dir = flag(args, "--dir")?;
            let addr = flag_or(args, "--addr", "127.0.0.1:8080");
            dual_sb_recovery::http::serve(&dir, &addr).map_err(|e| e.to_string())
        }
        "init" | "get" | "list" | "put" | "delete" => file_cmd(cmd, args),
        "demo" => demo_cmd(args.get(1..).unwrap_or(&[])),
        other => Err(format!("unknown command '{other}' (try 'help')")),
    }
}

fn file_cmd(cmd: &str, args: &[String]) -> Result<(), String> {
    let dir = flag(args, "--dir")?;
    let vfs = RealVfs::open(&dir).map_err(|e| e.to_string())?;
    let (mut repo, report) = Repository::open(vfs).map_err(|e| e.to_string())?;
    print_recovery(&report);

    match cmd {
        "init" => {
            println!(
                "repository ready: generation {} data_len {}",
                repo.generation(),
                repo.data_len()
            );
        }
        "put" => {
            let key = flag(args, "--key")?;
            let value = flag(args, "--value")?;
            let gen = repo
                .put(key.into_bytes(), value.into_bytes())
                .map_err(|e| e.to_string())?;
            println!("committed generation {gen}");
        }
        "delete" => {
            let key = flag(args, "--key")?;
            let gen = repo
                .delete(key.into_bytes())
                .map_err(|e| e.to_string())?;
            println!("committed generation {gen}");
        }
        "get" => {
            let key = flag(args, "--key")?;
            match repo.get(key.as_bytes()) {
                Some(v) => println!("{}", String::from_utf8_lossy(v)),
                None => return Err("key not found".into()),
            }
        }
        "list" => {
            for (k, v) in repo.entries() {
                println!("{k} = {v}");
            }
        }
        _ => unreachable!(),
    }
    Ok(())
}

fn print_recovery(report: &dual_sb_recovery::repo::RecoveryReport) {
    println!(
        "recovery: selected slot {:?} generation {:?}; data {} -> {} bytes ({} orphan truncated)",
        report.selected_slot,
        report.selected_generation,
        report.data_len_before,
        report.data_len_after,
        report.truncated_orphan_bytes
    );
    for s in &report.slots {
        println!(
            "  slot {} present={} accepted={}: {}",
            s.slot, s.present, s.accepted, s.detail
        );
    }
}

fn demo_cmd(args: &[String]) -> Result<(), String> {
    let sub = args.first().map(|s| s.as_str()).unwrap_or("");
    match sub {
        "matrix" | "matrix2" => {
            let cases = if sub == "matrix" {
                demo::run_matrix()
            } else {
                demo::run_round2_matrix()
            };
            let mut failed = 0usize;
            for c in &cases {
                println!(
                    "{:15} torn={:5} -> gen {:?} (want {}) value={:?} orphan={} {}",
                    c.point,
                    c.torn,
                    c.recovered_generation,
                    c.expected_generation,
                    c.last_value,
                    c.orphan_bytes_truncated,
                    if c.passed() { "PASS" } else { "FAIL" }
                );
                if !c.passed() {
                    failed += 1;
                }
            }
            println!(
                "\n{} / {} cases passed",
                cases.len() - failed,
                cases.len()
            );
            if failed > 0 {
                return Err(format!("{failed} cases failed"));
            }
            Ok(())
        }
        "scenarios" => {
            for s in demo::all_named() {
                println!("== {} ==", s.name);
                println!("   {}", s.description);
                println!("   -> {}", s.outcome);
                println!("{}", s.report.to_string_pretty());
            }
            Ok(())
        }
        "crash" => {
            let point = flag(args, "--point")?;
            let torn = flag_or(args, "--torn", "Half");
            let round: u64 = flag_or(args, "--round", "3").parse().map_err(|_| "bad --round")?;
            let point = demo::parse_point(&point)
                .ok_or_else(|| format!("unknown crash point '{point}'"))?;
            let torn = demo::parse_torn(&torn).ok_or("torn must be None|Half|Short")?;
            let c = demo::run_single(
                dual_sb_recovery::io::CrashPolicy::new(point, torn),
                round,
            );
            println!("{}", case_to_json(&c).to_string_pretty());
            if !c.passed() {
                return Err("case failed".into());
            }
            Ok(())
        }
        _ => Err("usage: demo matrix|matrix2|scenarios|crash ...".into()),
    }
}

fn case_to_json(c: &demo::CaseResult) -> dual_sb_recovery::json::Json {
    use dual_sb_recovery::json::Json;
    let slots = c
        .slot_reports
        .iter()
        .map(|(slot, present, accepted, detail)| {
            Json::Arr(vec![
                Json::n(*slot as u64),
                Json::Bool(*present),
                Json::Bool(*accepted),
                Json::s(detail.clone()),
            ])
        })
        .collect();
    Json::Obj(vec![
        ("point".into(), Json::s(c.point)),
        ("torn".into(), Json::s(c.torn)),
        ("recovered_generation".into(), match c.recovered_generation {
            Some(g) => Json::n(g),
            None => Json::Null,
        }),
        ("expected_generation".into(), Json::n(c.expected_generation)),
        ("value".into(), match &c.last_value {
            Some(v) => Json::s(v),
            None => Json::Null,
        }),
        ("orphan_bytes_truncated".into(), Json::n(c.orphan_bytes_truncated)),
        ("slots".into(), Json::Arr(slots)),
        ("passed".into(), Json::Bool(c.passed())),
    ])
}

fn flag(args: &[String], name: &str) -> Result<String, String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
        .ok_or_else(|| format!("missing {name}"))
}

fn flag_or(args: &[String], name: &str, default: &str) -> String {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
        .unwrap_or_else(|| default.to_string())
}
