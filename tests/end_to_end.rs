//! End-to-end integration tests against the real filesystem plus the
//! fault-injecting I/O layer.

use std::io::Read as _;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use extsort::engine::{Engine, SortConfig};
use extsort::format::Record;
use extsort::fs::{overwrite_at, FailState, FailingFs, RealFs};
use extsort::key::KeySpec;

fn temp_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "extsort-test-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_input(root: &Path, job: &str, lines: &[&str]) {
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    let mut body = String::new();
    for l in lines {
        body.push_str(l);
        body.push('\n');
    }
    std::fs::write(dir.join("input.txt"), body).unwrap();
}

fn read_output(root: &Path, job: &str) -> String {
    std::fs::read_to_string(root.join("jobs").join(job).join("output.txt")).unwrap()
}

fn reference_sort(lines: &[String], spec: &KeySpec) -> Vec<String> {
    let mut recs: Vec<Record> = lines
        .iter()
        .enumerate()
        .map(|(seq, l)| Record { seq: seq as u64, line: l.as_bytes().to_vec() })
        .collect();
    recs.sort_by(|a, b| spec.cmp(a, b));
    recs.into_iter().map(|r| String::from_utf8(r.line).unwrap()).collect()
}

fn split_lines(s: &str) -> Vec<String> {
    s.lines().map(|l| l.to_owned()).collect()
}

fn run_engine(root: &Path, job: &str, mem: usize, buf: usize, key: &str) -> extsort::SortStats {
    let fs: Arc<dyn extsort::fs::Fs> = Arc::new(RealFs::new(root.to_path_buf()));
    let engine = Engine::new(fs);
    let cfg = SortConfig {
        key: KeySpec::parse(key).unwrap(),
        mem,
        buf,
        fanin: Some(8),
    };
    engine.run(job, &cfg).unwrap()
}

// ---------------------------------------------------------------------------
// 1. Empty input
// ---------------------------------------------------------------------------

#[test]
fn empty_input_produces_empty_output() {
    let root = temp_dir("empty");
    write_input(&root, "j", &[]);
    let stats = run_engine(&root, "j", 4096, 512, "");
    assert_eq!(stats.input_records, 0);
    assert_eq!(stats.runs, 0);
    assert_eq!(read_output(&root, "j"), "");
}

// ---------------------------------------------------------------------------
// 2. Small memory forces many runs + multiple merge rounds; result must match
//    the in-memory reference sort.
// ---------------------------------------------------------------------------

fn deterministic_lines(n: usize) -> Vec<String> {
    // xorshift32 for reproducibility without a dependency on `rand`.
    let mut x: u32 = 0x1234_5678;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        let groups = [
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        ];
        let g = groups[(x as usize) % groups.len()];
        // Include a second numeric field and duplicate keys to exercise
        // composite ordering and stability.
        out.push(format!("{g}\t{:05}\tid-{i:05}", x % 7));
    }
    out
}

#[test]
fn forced_multi_round_merge_matches_reference() {
    let root = temp_dir("multi");
    let lines = deterministic_lines(400);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    let body: String = lines.iter().map(|l| format!("{l}\n")).collect();
    std::fs::write(dir.join("input.txt"), body).unwrap();

    // Tiny budget: ~200B for records after the I/O buffer -> dozens of runs;
    // fan-in 3 forces ceil(ceil(runs/3)/3)... multi-round merges.
    let stats = run_engine(&root, job, 2048, 256, "1:asc;2:asc");
    assert!(stats.runs >= 8, "expected many runs, got {}", stats.runs);
    assert!(stats.merge_rounds >= 2, "expected >=2 merge rounds, got {}", stats.merge_rounds);

    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("1:asc;2:asc").unwrap());
    assert_eq!(got.len(), want.len());
    assert_eq!(got, want, "external sort disagrees with in-memory reference");
}

// ---------------------------------------------------------------------------
// 3. Stability: duplicate composite keys keep input order
// ---------------------------------------------------------------------------

#[test]
fn duplicate_keys_keep_input_order() {
    let root = temp_dir("stable");
    let lines = vec!["same\tx", "same\tx", "same\tx", "a\tz", "same\tx"];
    write_input(&root, "j", &lines);
    run_engine(&root, "j", 800, 256, "1:asc");
    let got = split_lines(&read_output(&root, "j"));
    // "a\tz" first, then the four "same\tx" lines in input order.
    assert_eq!(got, vec!["a\tz", "same\tx", "same\tx", "same\tx", "same\tx"]);
}

#[test]
fn descending_and_composite_order_matches_reference() {
    let root = temp_dir("composite");
    let lines = deterministic_lines(120);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();
    run_engine(&root, job, 1500, 300, "2:desc;1:asc");
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("2:desc;1:asc").unwrap());
    assert_eq!(got, want);
}

// ---------------------------------------------------------------------------
// 4. One record much larger than the memory budget ("超大单行")
// ---------------------------------------------------------------------------

#[test]
fn oversized_single_line_is_sorted_correctly() {
    let root = temp_dir("oversize");
    let big = format!("huge\t{}", "Z".repeat(20_000));
    let smalls: Vec<String> = (0..20).map(|i| format!("small\t{i:03}")).collect();
    let mut lines: Vec<String> = smalls.clone();
    lines.push(big.clone()); // line far exceeds mem=2048
    lines.extend(smalls.iter().cloned());
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    let stats = run_engine(&root, job, 2048, 256, "1:asc");
    assert!(stats.runs >= 2);
    let got = split_lines(&read_output(&root, job));
    let mut want = lines.clone();
    want.sort();
    // whole-field ascending via field1: all "huge" < "small"
    let want_ref = reference_sort(&lines, &KeySpec::parse("1:asc").unwrap());
    let _ = want;
    assert_eq!(got.len(), lines.len());
    assert_eq!(got, want_ref);
    assert!(got.contains(&big));
}

// ---------------------------------------------------------------------------
// 5. Recovery after an injected fsync failure mid phase A
// ---------------------------------------------------------------------------

#[test]
fn recovers_from_injected_sync_failure_in_run_phase() {
    let root = temp_dir("syncfail");
    let lines = deterministic_lines(200);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    let state = FailState::new();
    {
        let mut s = state.lock().unwrap();
        s.fail_on_sync = Some(2); // die during the 2nd segment fsync
    }
    let first = {
        let fs: Arc<dyn extsort::fs::Fs> =
            Arc::new(FailingFs::new(Arc::new(RealFs::new(root.to_path_buf())), state.clone()));
        let engine = Engine::new(fs);
        let cfg = SortConfig {
            key: KeySpec::parse("1:asc;2:asc").unwrap(),
            mem: 1600,
            buf: 256,
            fanin: Some(4),
        };
        engine.run(job, &cfg)
    };
    assert!(first.is_err(), "injected failure must surface as an error");

    // Resume on a clean filesystem: exactly one segment must be reused.
    let fs: Arc<dyn extsort::fs::Fs> = Arc::new(RealFs::new(root.to_path_buf()));
    let engine = Engine::new(fs);
    let cfg = SortConfig {
        key: KeySpec::parse("1:asc;2:asc").unwrap(),
        mem: 1600,
        buf: 256,
        fanin: Some(4),
    };
    let stats = engine.run(job, &cfg).unwrap();
    assert!(stats.runs_reused >= 1, "at least the run committed before the crash is reused");
    // fsync #2 failed, so exactly one run can have been committed beforehand.
    assert_eq!(stats.runs_reused, 1, "only the run fsynced before the fault is reusable");
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("1:asc;2:asc").unwrap());
    assert_eq!(got, want);
}

// ---------------------------------------------------------------------------
// 6. Recovery after an injected fsync failure during the merge phase
// ---------------------------------------------------------------------------

#[test]
fn recovers_from_injected_sync_failure_in_merge_phase() {
    let root = temp_dir("mergefail");
    let lines = deterministic_lines(200);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    let form = SortConfig {
        key: KeySpec::parse("1:asc;2:asc").unwrap(),
        mem: 1600,
        buf: 256,
        fanin: Some(3),
    };

    // Phase A once, then strip merge/DONE state from the manifest so the
    // following attempts redo only the merge phase.
    Engine::new(Arc::new(RealFs::new(root.to_path_buf())))
        .run(job, &form)
        .unwrap();
    std::fs::remove_dir_all(root.join("jobs").join(job).join("merges")).unwrap();
    let manifest_path = root.join("jobs").join(job).join("manifest.txt");
    let body = std::fs::read_to_string(&manifest_path).unwrap();
    let stripped: String = body
        .lines()
        .filter(|l| !l.starts_with("merge ") && *l != "state DONE")
        .map(|l| format!("{l}\n"))
        .collect();
    std::fs::write(&manifest_path, stripped).unwrap();

    // Injected failure on the 2nd merge-segment sync: the first merge output
    // must already be committed and reusable afterwards.
    let state = FailState::new();
    {
        let mut s = state.lock().unwrap();
        s.fail_on_sync = Some(2);
    }
    let attempt = {
        let fs: Arc<dyn extsort::fs::Fs> =
            Arc::new(FailingFs::new(Arc::new(RealFs::new(root.to_path_buf())), state.clone()));
        Engine::new(fs).run(job, &form)
    };
    assert!(attempt.is_err(), "injected merge-sync failure must surface");
    {
        let s = state.lock().unwrap();
        assert!(s.sync_fired, "the fault should actually have fired");
    }

    let stats = Engine::new(Arc::new(RealFs::new(root.to_path_buf())))
        .run(job, &form)
        .unwrap();
    assert_eq!(stats.runs_reused, stats.runs, "all runs are reused after a merge crash");
    assert!(stats.merges_reused >= 1, "first merge output should survive, got {}", stats.merges_reused);
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("1:asc;2:asc").unwrap());
    assert_eq!(got, want);
}

/// Crash simulation that keeps 2 committed merge files and drops later ones.
#[test]
fn resumes_from_partial_merge_prefix() {
    let root = temp_dir("mergefail");
    let lines = deterministic_lines(200);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    // Run formation budget forces ~10 runs; fan-in 3 -> multiple rounds and
    // multiple merge steps.
    let form = SortConfig {
        key: KeySpec::parse("1:asc;2:asc").unwrap(),
        mem: 1600,
        buf: 256,
        fanin: Some(3),
    };

    // Complete the whole job once to create run + merge files.
    Engine::new(Arc::new(RealFs::new(root.to_path_buf())))
        .run(job, &form)
        .unwrap();
    let run_count = std::fs::read_dir(root.join("jobs").join(job).join("runs"))
        .unwrap()
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_string_lossy().ends_with(".exs"))
        .count();
    assert!(run_count >= 5, "need several runs, got {run_count}");

    // Simulate a crash after several merge steps committed: keep the first
    // two merge files but truncate the manifest to drop later merges and the
    // DONE marker, then corrupt nothing — resume must reuse the surviving
    // prefix. (The partial-merge-fsync case is covered at the run level by
    // the dedicated sync-injection test and by .tmp cleanup on startup.)
    let merges_dir = root.join("jobs").join(job).join("merges");
    let mut merge_names: Vec<String> = std::fs::read_dir(&merges_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".exs"))
        .collect();
    merge_names.sort();
    assert!(merge_names.len() >= 3, "need several merge steps, got {}", merge_names.len());
    for n in &merge_names[2..] {
        std::fs::remove_file(merges_dir.join(n)).unwrap();
    }

    let manifest = root.join("jobs").join(job).join("manifest.txt");
    let body = std::fs::read_to_string(&manifest).unwrap();
    let mut kept_merges = 0usize;
    let stripped: String = body
        .lines()
        .filter(|l| {
            if l.starts_with("merge ") {
                if kept_merges < 2 {
                    kept_merges += 1;
                    true
                } else {
                    false
                }
            } else {
                *l != "state DONE"
            }
        })
        .map(|l| format!("{l}\n"))
        .collect();
    assert_eq!(kept_merges, 2);
    std::fs::write(&manifest, stripped).unwrap();

    let stats = Engine::new(Arc::new(RealFs::new(root.to_path_buf())))
        .run(job, &form)
        .unwrap();
    assert_eq!(stats.merges_reused, 2, "the surviving merge prefix is reused");
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("1:asc;2:asc").unwrap());
    assert_eq!(got, want);
}

// ---------------------------------------------------------------------------
// 7. Corrupt temp/run file on disk -> detected by CRC; job rebuilds
// ---------------------------------------------------------------------------

#[test]
fn corrupted_run_file_is_detected_and_rebuilt() {
    let root = temp_dir("corrupt");
    let lines = deterministic_lines(120);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    let cfg = SortConfig {
        key: KeySpec::parse("1:asc;2:asc").unwrap(),
        mem: 1500,
        buf: 256,
        fanin: Some(4),
    };
    Engine::new(Arc::new(RealFs::new(root.to_path_buf())))
        .run(job, &cfg)
        .unwrap();

    // Corrupt a payload byte inside the second run (offset 64 skips magic).
    let runs_dir = root.join("jobs").join(job).join("runs");
    let mut names: Vec<String> = std::fs::read_dir(&runs_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".exs"))
        .collect();
    names.sort();
    assert!(names.len() >= 2);
    let target = names[1].clone();
    let p = runs_dir.join(&target);
    let mut data = std::fs::read(&p).unwrap();
    data[64] ^= 0xFF;
    std::fs::write(&p, data).unwrap();

    // Direct verification must flag the corruption.
    let fs: Arc<dyn extsort::fs::Fs> = Arc::new(RealFs::new(root.to_path_buf()));
    let rel = format!("jobs/{job}/runs/{target}");
    assert!(extsort::format::verify_run(fs.as_ref(), &rel, 256).is_err());

    // Re-running rebuilds from the corrupt run onward and still succeeds.
    let stats = Engine::new(fs).run(job, &cfg).unwrap();
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::parse("1:asc;2:asc").unwrap());
    assert_eq!(got, want);
    assert!(stats.runs_reused <= names.len() as u64 - 2);
}

#[test]
fn leftover_temp_file_is_cleaned_and_sorting_still_works() {
    let root = temp_dir("tmpstale");
    let lines: Vec<String> = (0..50).map(|i| format!("line-{i:03}")).collect();
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(dir.join("runs")).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();
    // Simulate a crashed spill leaving a temp file behind.
    std::fs::write(dir.join("runs").join("run-000001.tmp"), b"EXTSORT1garbage").unwrap();

    run_engine(&root, job, 1200, 256, "");
    let got = split_lines(&read_output(&root, job));
    let mut want = lines.clone();
    want.sort();
    assert_eq!(got, want);
}

// ---------------------------------------------------------------------------
// 8. Conflicting config on resume is rejected
// ---------------------------------------------------------------------------

#[test]
fn conflicting_config_on_resume_is_rejected() {
    let root = temp_dir("conflict");
    write_input(&root, "j", &["b", "a"]);
    run_engine(&root, "j", 4096, 512, "");
    let fs: Arc<dyn extsort::fs::Fs> = Arc::new(RealFs::new(root.to_path_buf()));
    let err = Engine::new(fs)
        .run(
            "j",
            &SortConfig { key: KeySpec::parse("1:desc").unwrap(), mem: 4096, buf: 512, fanin: Some(8) },
        )
        .unwrap_err();
    assert!(matches!(err, extsort::Error::ConflictingJob(_)), "got {err:?}");
}

// ---------------------------------------------------------------------------
// 9. HTTP end-to-end smoke test (small memory, response headers, resume GET)
// ---------------------------------------------------------------------------

fn http_send(addr: &str, req: &str, body: Option<&[u8]>) -> (u16, Vec<(String, String)>, Vec<u8>) {
    let mut stream = TcpStream::connect(addr).unwrap();
    use std::io::Write as _;
    stream.write_all(req.as_bytes()).unwrap();
    if let Some(b) = body {
        stream.write_all(b).unwrap();
    }
    stream.flush().unwrap();

    let mut all = Vec::new();
    stream.read_to_end(&mut all).unwrap();
    let split = all.windows(4).position(|w| w == b"\r\n\r\n").unwrap() + 4;
    let head = String::from_utf8_lossy(&all[..split]).to_string();
    let mut lines = head.lines();
    let status: u16 = lines.next().unwrap().split_whitespace().nth(1).unwrap().parse().unwrap();
    let mut headers = Vec::new();
    for l in lines {
        if let Some((k, v)) = l.split_once(':') {
            headers.push((k.trim().to_ascii_lowercase(), v.trim().to_owned()));
        }
    }
    (status, headers, all[split..].to_vec())
}

#[test]
fn http_sort_resume_and_headers() {
    let root = temp_dir("http");
    let server = extsort::server::Server::bind("127.0.0.1:0", &root).unwrap();
    let addr = server.local_addr().unwrap();
    std::thread::spawn(move || server.run().unwrap());

    let body = "z\t1\ny\t2\nx\t1\nw\t1\n".to_string();
    let req = format!(
        "POST /sort?job=h1&mem=300&buf=128&key=2%3Aasc%3B1%3Aasc HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let (status, headers, resp) = http_send(&addr.to_string(), &req, Some(body.as_bytes()));
    assert_eq!(status, 200);
    let h = |name: &str| headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone()).unwrap();
    assert_eq!(h("x-sort-records"), "4");
    let runs: u64 = h("x-sort-runs").parse().unwrap();
    assert!(runs >= 2, "small budget should force multiple runs: {runs}");
    assert_eq!(
        String::from_utf8(resp).unwrap(),
        "w\t1\nx\t1\nz\t1\ny\t2\n"
    );

    // GET resume of the finished job
    let req = "GET /sort?job=h1&mem=300&buf=128&key=2%3Aasc%3B1%3Aasc HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n";
    let (status, _, resp) = http_send(&addr.to_string(), req, None);
    assert_eq!(status, 200);
    assert_eq!(String::from_utf8(resp).unwrap(), "w\t1\nx\t1\nz\t1\ny\t2\n");

    // Health + 404 + 400
    let (s, _, b) = http_send(&addr.to_string(), "GET /healthz HTTP/1.1\r\nConnection: close\r\n\r\n", None);
    assert_eq!(s, 200);
    assert_eq!(b, b"ok\n");
    let (s, _, _) = http_send(&addr.to_string(), "GET /nope HTTP/1.1\r\nConnection: close\r\n\r\n", None);
    assert_eq!(s, 404);
    let (s, _, _) = http_send(
        &addr.to_string(),
        "POST /sort?job=bad%2Fid&mem=10 HTTP/1.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        None,
    );
    assert_eq!(s, 400);
}

// ---------------------------------------------------------------------------
// 10. verify_run catches a corrupted trailer
// ---------------------------------------------------------------------------

#[test]
fn trailer_corruption_is_detected() {
    let root = temp_dir("trailer");
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), "b\na\nc\n").unwrap();
    run_engine(&root, job, 1200, 256, "");

    let runs_dir = dir.join("runs");
    let name = std::fs::read_dir(&runs_dir)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .file_name()
        .to_string_lossy()
        .into_owned();
    let p = runs_dir.join(&name);
    let len = p.metadata().unwrap().len();
    let mut data = std::fs::read(&p).unwrap();
    // Flip a byte in the trailer (last 8 bytes).
    *data.last_mut().unwrap() ^= 0xFF;
    std::fs::write(&p, &data).unwrap();
    assert_eq!(len as usize, data.len());

    let fs: Arc<dyn extsort::fs::Fs> = Arc::new(RealFs::new(root.to_path_buf()));
    let rel = format!("jobs/{job}/runs/{name}");
    assert!(extsort::format::verify_run(fs.as_ref(), &rel, 256).is_err());
}

#[test]
fn injected_read_failure_can_be_resumed() {
    let root = temp_dir("readfail");
    let lines = deterministic_lines(120);
    let job = "j";
    let dir = root.join("jobs").join(job);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("input.txt"), lines.iter().map(|l| format!("{l}\n")).collect::<String>()).unwrap();

    let state = Arc::new(Mutex::new(extsort::fs::FailState::default()));
    {
        let mut s = state.lock().unwrap();
        s.fail_read_path = Some("input.txt".into());
        s.fail_on_read_bytes = Some(1500); // fail mid input, after a run or two
    }
    let attempt = {
        let fs: Arc<dyn extsort::fs::Fs> =
            Arc::new(FailingFs::new(Arc::new(RealFs::new(root.to_path_buf())), state));
        Engine::new(fs).run(
            job,
            &SortConfig { key: KeySpec::default_key(), mem: 1500, buf: 256, fanin: Some(4) },
        )
    };
    assert!(attempt.is_err());

    // Resume cleanly.
    let _stats = run_engine(&root, job, 1500, 256, "");
    let got = split_lines(&read_output(&root, job));
    let want = reference_sort(&lines, &KeySpec::default_key());
    assert_eq!(got, want);
}

#[allow(dead_code)]
fn use_overwrite(fs: &dyn extsort::fs::Fs, rel: &str) {
    let _ = overwrite_at(fs, rel, 0, &[0]);
}
