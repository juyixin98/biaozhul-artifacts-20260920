//! End-to-end determinism across host time zone and file mtime.
//!
//! The test builds three byte-identical trees, gives their files wildly
//! different mtimes, and runs the *real* server binary's CLI under different
//! `TZ` values. Archive digests must match.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use tempfile::TempDir;

fn bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_repro-pack"))
}

fn write_file(path: &Path, bytes: &[u8]) {
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

fn build_tree(root: &Path) {
    write_file(&root.join("docs/readme.md"), b"# demo\n");
    write_file(&root.join("bin/tool"), b"script\n");
    write_file(&root.join("data.bin"), &[0u8, 1, 2, 3, 255]);
    write_file(&root.join("empty"), b"");
    fs::create_dir_all(root.join("empty-dir/sub")).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(root.join("bin/tool"), fs::Permissions::from_mode(0o755)).unwrap();
    }
    std::os::unix::fs::symlink("../docs/readme.md", root.join("bin/readme-link")).unwrap();
}

fn set_mtimes(root: &Path, date: &str) {
    // touch all non-symlink entries (find -not -type l)
    let out = Command::new("find")
        .arg(root)
        .args(["-not", "-type", "l"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let listing = String::from_utf8(out.stdout).unwrap();
    for line in listing.lines() {
        let status = Command::new("touch")
            .args(["-h", "-d", date])
            .arg(line)
            .status()
            .unwrap();
        assert!(status.success(), "touch failed for {line}");
    }
}

fn pack_cli(src: &Path, out: &Path, tz: &str) -> String {
    fs::create_dir_all(out).unwrap();
    let output = Command::new(bin())
        .args([
            "pack",
            "--source",
            src.to_str().unwrap(),
            "--output-dir",
            out.to_str().unwrap(),
            "--name",
            "tz",
        ])
        .env("TZ", tz)
        .env_remove("SOURCE_DATE_EPOCH")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "pack failed under TZ={tz}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).unwrap();
    let v: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    v["sha256"].as_str().unwrap().to_string()
}

#[test]
fn timezone_and_mtime_do_not_affect_archive_bytes() {
    let td = TempDir::new().unwrap();
    let s1 = td.path().join("s1");
    let s2 = td.path().join("s2");
    let s3 = td.path().join("s3");
    for s in [&s1, &s2, &s3] {
        fs::create_dir_all(s).unwrap();
        build_tree(s);
    }
    // Different epochs, different time zones, different DST eras.
    set_mtimes(&s1, "1990-01-01 00:00:00 +0000");
    set_mtimes(&s2, "2038-07-04 12:34:56 -0800");
    set_mtimes(&s3, "2001-09-09 01:46:40 +0530");

    let h1 = pack_cli(&s1, &td.path().join("o1"), "UTC");
    let h2 = pack_cli(&s2, &td.path().join("o2"), "America/Los_Angeles");
    let h3 = pack_cli(&s3, &td.path().join("o3"), "Asia/Kolkata");

    assert_eq!(h1, h2, "mtime/time zone changed archive bytes");
    assert_eq!(h2, h3);

    // And the raw tar bytes themselves.
    let b1 = fs::read(td.path().join("o1/tz.tar")).unwrap();
    let b2 = fs::read(td.path().join("o2/tz.tar")).unwrap();
    let b3 = fs::read(td.path().join("o3/tz.tar")).unwrap();
    assert_eq!(b1, b2);
    assert_eq!(b2, b3);

    // External tool sanity: the archive extracts cleanly.
    let x = td.path().join("extract");
    fs::create_dir_all(&x).unwrap();
    let st = Command::new("tar")
        .args(["xf", td.path().join("o1/tz.tar").to_str().unwrap()])
        .current_dir(&x)
        .status()
        .unwrap();
    assert!(st.success());
    assert_eq!(fs::read(x.join("docs/readme.md")).unwrap(), b"# demo\n");
}

#[test]
fn server_bind_and_http_request_end_to_end() {
    use std::io::{Read, Write};
    use std::net::TcpListener;

    let td = TempDir::new().unwrap();
    let src = td.path().join("src");
    let out = td.path().join("out");
    fs::create_dir_all(&src).unwrap();
    write_file(&src.join("ping.txt"), b"pong\n");

    let port = {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let addr = format!("127.0.0.1:{port}");
    let mut child = Command::new(bin())
        .args(["serve", "--bind", &addr])
        .spawn()
        .unwrap();

    // Wait for readiness.
    let mut ready = false;
    for _ in 0..100 {
        if let Ok(mut stream) = std::net::TcpStream::connect(&addr) {
            let req = b"GET /health HTTP/1.0\r\n\r\n";
            stream.write_all(req).unwrap();
            let mut resp = String::new();
            stream.read_to_string(&mut resp).unwrap();
            if resp.contains("\"status\":\"ok\"") {
                ready = true;
                break;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    assert!(ready, "server never became ready");

    let body = format!(
        r#"{{"source":"{}","output_dir":"{}","artifact_name":"live"}}"#,
        src.display(),
        out.display()
    );
    let mut stream = std::net::TcpStream::connect(&addr).unwrap();
    stream
        .write_all(
            format!(
                "POST /pack HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .as_bytes(),
        )
        .unwrap();
    let mut resp = String::new();
    stream.read_to_string(&mut resp).unwrap();
    assert!(resp.contains("\"sha256\""), "pack response: {resp}");
    assert!(out.join("live.tar").exists());

    child.kill().unwrap();
    child.wait().unwrap();
}
