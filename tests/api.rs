//! 端到端测试：真实 HTTP 服务器（子进程），含崩溃重启场景。
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::Value;

const BIN: &str = env!("CARGO_BIN_EXE_chunked-upload");

struct TestServer {
    child: Child,
    base: String,
    data_dir: PathBuf,
    /// 重启交接时，旧值 drop 不得删除数据目录
    keep_dir: bool,
}

impl TestServer {
    fn start(tag: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "chunked-upload-test-{tag}-{}-{}",
            std::process::id(),
            uuid_like()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let port = free_port();
        let child = Command::new(BIN)
            .env("LISTEN_ADDR", format!("127.0.0.1:{port}"))
            .env("DATA_DIR", &dir)
            .env("MAX_CHUNK_SIZE", "8388608") // 8 MiB
            .env("RUST_LOG", "warn,chunked_upload=info")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("failed to spawn server binary");
        let s = TestServer {
            child,
            base: format!("http://127.0.0.1:{port}"),
            data_dir: dir,
            keep_dir: false,
        };
        s.wait_ready();
        s
    }

    fn wait_ready(&self) {
        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            if reqwest::blocking::get(format!("{}/healthz", self.base)).is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("server did not become ready");
    }

    fn stop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    /// SIGKILL 模拟崩溃（不优雅退出）
    fn kill(&mut self) {
        #[cfg(unix)]
        {
            let pid = self.child.id() as i32;
            unsafe {
                libc_kill(pid);
            }
            let _ = self.child.wait();
        }
        #[cfg(not(unix))]
        self.stop();
    }

    /// 在同一 data_dir 上重启
    fn restart(mut self) -> Self {
        self.kill();
        let data_dir = self.data_dir.clone();
        self.keep_dir = true; // 旧值 drop 时保留数据目录
        drop(self);
        let port = free_port();
        let child = Command::new(BIN)
            .env("LISTEN_ADDR", format!("127.0.0.1:{port}"))
            .env("DATA_DIR", &data_dir)
            .env("MAX_CHUNK_SIZE", "8388608")
            .env("RUST_LOG", "warn,chunked=info")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("failed to re-spawn server binary");
        let s = TestServer {
            child,
            base: format!("http://127.0.0.1:{port}"),
            data_dir,
            keep_dir: false,
        };
        s.wait_ready();
        s
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        self.stop();
        if !self.keep_dir {
            let _ = std::fs::remove_dir_all(&self.data_dir);
        }
    }
}

#[cfg(unix)]
unsafe fn libc_kill(pid: i32) {
    // 直接用 syscall 常量，避免给 dev-dependencies 加 libc
    extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    let _ = kill(pid, 9);
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn uuid_like() -> u32 {
    use std::time::SystemTime;
    SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .subsec_nanos()
}

// ---------- 测试工具 ----------

struct Session {
    upload_id: String,
    chunk_size: usize,
    chunk_sizes: Vec<u64>,
    data: Vec<u8>,
    sha: String,
}

fn make_data(total: usize, chunk_size: usize) -> Session {
    // 确定性但跨块变化的数据
    let mut data = Vec::with_capacity(total);
    let mut x: u32 = 0x1234_5678;
    while data.len() < total {
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        data.push((x & 0xff) as u8);
    }
    data.truncate(total);
    let sha = sha256_hex(&data);
    let chunk_sizes = data.chunks(chunk_size).map(|c| c.len() as u64).collect();
    Session {
        upload_id: String::new(),
        chunk_size,
        chunk_sizes,
        data,
        sha,
    }
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

fn create_upload(base: &str, s: &mut Session) {
    let resp = reqwest::blocking::Client::new()
        .post(format!("{base}/uploads"))
        .json(&serde_json::json!({
            "sha256": s.sha,
            "total_size": s.data.len(),
            "chunk_sizes": s.chunk_sizes,
        }))
        .send()
        .unwrap();
    assert_eq!(resp.status(), 201, "create failed: {:?}", resp.text());
    let v: Value = resp.json().unwrap();
    s.upload_id = v["upload_id"].as_str().unwrap().to_string();
}

fn put_chunk_raw(base: &str, s: &Session, index: usize) -> reqwest::blocking::Response {
    let start = index * s.chunk_size;
    let end = (start + s.chunk_size).min(s.data.len());
    let body = s.data[start..end].to_vec();
    let sha = sha256_hex(&body);
    reqwest::blocking::Client::new()
        .put(format!(
            "{}/uploads/{}/chunks/{}?sha256={}",
            base, s.upload_id, index, sha
        ))
        .header("Content-Type", "application/octet-stream")
        .body(body)
        .send()
        .unwrap()
}

fn finish(base: &str, s: &Session) -> reqwest::blocking::Response {
    reqwest::blocking::Client::new()
        .post(format!("{}/uploads/{}/finish", base, s.upload_id))
        .send()
        .unwrap()
}

fn object_url(base: &str, sha: &str) -> String {
    format!("{base}/objects/{sha}")
}

// ---------- 测试用例 ----------

#[test]
fn healthz_ok() {
    let srv = TestServer::start("health");
    let resp = reqwest::blocking::get(format!("{}/healthz", srv.base)).unwrap();
    assert_eq!(resp.status(), 200);
    let v: Value = resp.json().unwrap();
    assert_eq!(v["status"], "ok");
}

#[test]
fn out_of_order_upload_then_download() {
    let srv = TestServer::start("ooo");
    let mut s = make_data(1_000_000, 256 * 1024); // 4 块
    create_upload(&srv.base, &mut s);

    // 乱序：3, 1, 0, 2
    for &i in &[3usize, 1, 0, 2] {
        let r = put_chunk_raw(&srv.base, &s, i);
        assert_eq!(r.status(), 201, "chunk {i}: {:?}", r.text());
    }

    // 发布前对象不可读
    let pre = reqwest::blocking::get(object_url(&srv.base, &s.sha)).unwrap();
    assert_eq!(
        pre.status(),
        404,
        "object must not be readable before finish"
    );

    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 200, "finish: {:?}", r.text());
    let v: Value = r.json().unwrap();
    assert_eq!(v["object_id"], s.sha);

    // 下载校验
    let got = reqwest::blocking::get(object_url(&srv.base, &s.sha))
        .unwrap()
        .bytes()
        .unwrap();
    assert_eq!(got.as_ref(), s.data.as_slice());

    // HEAD
    let head = reqwest::blocking::Client::new()
        .head(object_url(&srv.base, &s.sha))
        .send()
        .unwrap();
    assert_eq!(head.status(), 200);
    assert_eq!(head.headers()["content-length"], s.data.len().to_string());
    assert_eq!(head.headers()["x-object-sha256"], s.sha);
}

#[test]
fn finish_with_missing_chunk_rejected() {
    let srv = TestServer::start("missing");
    let mut s = make_data(500_000, 128 * 1024); // 4 块
    create_upload(&srv.base, &mut s);
    for i in [0usize, 1, 3] {
        // 跳过 2
        let r = put_chunk_raw(&srv.base, &s, i);
        assert_eq!(r.status(), 201);
    }
    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 400, "finish with missing chunk must be 400");
    let v: Value = r.json().unwrap();
    assert_eq!(v["error"]["code"], "bad_request");

    // 会话状态显示缺失块
    let st: Value = reqwest::blocking::get(format!("{}/uploads/{}", srv.base, s.upload_id))
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(st["missing_chunks"][0], 2);

    // 对象仍不可读
    let pre = reqwest::blocking::get(object_url(&srv.base, &s.sha)).unwrap();
    assert_eq!(pre.status(), 404);

    // 补齐后可以完成
    let r = put_chunk_raw(&srv.base, &s, 2);
    assert_eq!(r.status(), 201);
    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 200);
}

#[test]
fn same_content_reupload_idempotent_different_content_rejected() {
    let srv = TestServer::start("idem");
    let mut s = make_data(300_000, 128 * 1024);
    create_upload(&srv.base, &mut s);

    let r = put_chunk_raw(&srv.base, &s, 0);
    assert_eq!(r.status(), 201);
    // 同内容重传 -> 200 + idempotent
    let r = put_chunk_raw(&srv.base, &s, 0);
    assert_eq!(r.status(), 200);
    let v: Value = r.json().unwrap();
    assert_eq!(v["idempotent"], true);

    // 异内容（伪造 query 哈希，内容仍是第 0 块但宣称成别的 sha）
    let start = 0;
    let end = s.chunk_size.min(s.data.len());
    let body = s.data[start..end].to_vec();
    let fake_sha = sha256_hex(&vec![0u8; body.len()]); // 全零块的哈希
    let r = reqwest::blocking::Client::new()
        .put(format!(
            "{}/uploads/{}/chunks/0?sha256={}",
            srv.base, s.upload_id, fake_sha
        ))
        .body(body)
        .send()
        .unwrap();
    assert_eq!(r.status(), 409, "different content must be rejected");

    // 用另一个合法对象的真块冒充：内容大小相同、哈希不同
    let other = make_data(300_000, 128 * 1024);
    let other_chunk0 = {
        let end = s.chunk_size.min(s.data.len());
        other.data[0..end].to_vec()
    };
    let other_sha = sha256_hex(&other_chunk0);
    if other_sha != sha256_hex(&s.data[0..s.chunk_size.min(s.data.len())]) {
        let r = reqwest::blocking::Client::new()
            .put(format!(
                "{}/uploads/{}/chunks/0?sha256={}",
                srv.base, s.upload_id, other_sha
            ))
            .body(other_chunk0)
            .send()
            .unwrap();
        assert_eq!(r.status(), 409);
    }
}

#[test]
fn wrong_chunk_sha256_rejected_and_not_recorded() {
    let srv = TestServer::start("badsha");
    let mut s = make_data(300_000, 128 * 1024);
    create_upload(&srv.base, &mut s);

    let end = s.chunk_size.min(s.data.len());
    let mut body = s.data[0..end].to_vec();
    body[0] ^= 0xff; // 破坏内容
    let declared = sha256_hex(&s.data[0..end]); // 声明的是原哈希
    let r = reqwest::blocking::Client::new()
        .put(format!(
            "{}/uploads/{}/chunks/0?sha256={}",
            srv.base, s.upload_id, declared
        ))
        .body(body)
        .send()
        .unwrap();
    assert_eq!(r.status(), 409);

    // 状态里不应有 0 号块
    let st: Value = reqwest::blocking::get(format!("{}/uploads/{}", srv.base, s.upload_id))
        .unwrap()
        .json()
        .unwrap();
    assert!(st["chunks"].get("0").is_none());

    // 原块仍可正常上传
    let r = put_chunk_raw(&srv.base, &s, 0);
    assert_eq!(r.status(), 201);
}

#[test]
fn whole_object_hash_mismatch_rejected() {
    let srv = TestServer::start("wholehash");
    // 声明假整体哈希，分块各自的哈希真实
    let mut s = make_data(300_000, 128 * 1024);
    s.sha = "a".repeat(64);
    create_upload(&srv.base, &mut s);
    let n = s.chunk_sizes.len();
    for i in 0..n {
        let r = put_chunk_raw(&srv.base, &s, i);
        assert_eq!(r.status(), 201);
    }
    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 409, "whole-hash mismatch must be 409");
    let v: Value = r.json().unwrap();
    assert_eq!(v["error"]["code"], "conflict");

    // 未发布，不可读；且修正会话只能新建（不可变对象语义）
    let pre = reqwest::blocking::get(object_url(&srv.base, &"a".repeat(64))).unwrap();
    assert_eq!(pre.status(), 404);
}

#[test]
fn duplicate_finish_is_idempotent() {
    let srv = TestServer::start("dupfinish");
    let mut s = make_data(300_000, 128 * 1024);
    create_upload(&srv.base, &mut s);
    for i in 0..s.chunk_sizes.len() {
        assert_eq!(put_chunk_raw(&srv.base, &s, i).status(), 201);
    }
    let r1 = finish(&srv.base, &s);
    assert_eq!(r1.status(), 200);
    let v1: Value = r1.json().unwrap();
    assert_eq!(v1["idempotent"], false);

    let r2 = finish(&srv.base, &s);
    assert_eq!(r2.status(), 200);
    let v2: Value = r2.json().unwrap();
    assert_eq!(v2["idempotent"], true);
    assert_eq!(v2["object_id"], s.sha);

    // 完成后再传已存在的同内容块仍然幂等
    let r3 = put_chunk_raw(&srv.base, &s, 0);
    assert_eq!(r3.status(), 200);
}

#[test]
fn crash_after_some_chunks_restart_resumes() {
    let srv = TestServer::start("crash1");
    let mut s = make_data(700_000, 256 * 1024); // 3 块
    create_upload(&srv.base, &mut s);
    assert_eq!(put_chunk_raw(&srv.base, &s, 0).status(), 201);
    assert_eq!(put_chunk_raw(&srv.base, &s, 1).status(), 201);

    // 发布前崩溃（SIGKILL）
    let srv = srv.restart();

    // 重启后已传块仍在，缺块可见，发布前仍不可读
    let st: Value = reqwest::blocking::get(format!("{}/uploads/{}", srv.base, s.upload_id))
        .unwrap()
        .json()
        .unwrap();
    assert!(st["chunks"].get("0").is_some());
    assert!(st["chunks"].get("1").is_some());
    assert_eq!(st["missing_chunks"][0], 2);
    let pre = reqwest::blocking::get(object_url(&srv.base, &s.sha)).unwrap();
    assert_eq!(pre.status(), 404);

    // 续传最后一块并完成
    assert_eq!(put_chunk_raw(&srv.base, &s, 2).status(), 201);
    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 200, "{:?}", r.text());

    let got = reqwest::blocking::get(object_url(&srv.base, &s.sha))
        .unwrap()
        .bytes()
        .unwrap();
    assert_eq!(got.as_ref(), s.data.as_slice());
}

#[test]
fn crash_between_chunk_rename_and_meta_update_adopts_orphan() {
    // 手工构造“块文件已落盘、meta 未记录”的崩溃现场
    let srv = TestServer::start("crash2");
    let mut s = make_data(700_000, 256 * 1024);
    create_upload(&srv.base, &mut s);
    assert_eq!(put_chunk_raw(&srv.base, &s, 0).status(), 201);

    // 停服后写入孤儿分块 1.part（合法内容），并留一个 .tmp 垃圾
    let mut srv2 = srv;
    srv2.kill();
    let up_dir = srv2.data_dir.join("uploads").join(&s.upload_id);
    let chunk1 = &s.data[s.chunk_size..(2 * s.chunk_size).min(s.data.len())];
    std::fs::write(up_dir.join("1.part"), chunk1).unwrap();
    std::fs::write(up_dir.join("2.part.tmp"), b"junk").unwrap();
    let data_dir = srv2.data_dir.clone();
    srv2.keep_dir = true;
    drop(srv2);
    // 端口/进程结构重建（start 的反向操作：复用 data_dir）
    let port = free_port();
    let child = Command::new(BIN)
        .env("LISTEN_ADDR", format!("127.0.0.1:{port}"))
        .env("DATA_DIR", &data_dir)
        .env("MAX_CHUNK_SIZE", "8388608")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut srv = TestServer {
        child,
        base: format!("http://127.0.0.1:{port}"),
        data_dir,
        keep_dir: false,
    };
    srv.wait_ready();

    // 孤儿块 1 应被收养，.tmp 被清理
    let st: Value = reqwest::blocking::get(format!("{}/uploads/{}", srv.base, s.upload_id))
        .unwrap()
        .json()
        .unwrap();
    assert!(
        st["chunks"].get("1").is_some(),
        "orphan chunk should be adopted"
    );
    assert_eq!(st["missing_chunks"][0], 2);
    assert!(!up_dir.join("2.part.tmp").exists());

    assert_eq!(put_chunk_raw(&srv.base, &s, 2).status(), 201);
    assert_eq!(finish(&srv.base, &s).status(), 200);
    let got = reqwest::blocking::get(object_url(&srv.base, &s.sha))
        .unwrap()
        .bytes()
        .unwrap();
    assert_eq!(got.as_ref(), s.data.as_slice());
    srv.stop();
}

#[test]
fn crash_after_commit_object_remains_readable_after_restart() {
    let srv = TestServer::start("crash3");
    let mut s = make_data(400_000, 256 * 1024);
    create_upload(&srv.base, &mut s);
    for i in 0..s.chunk_sizes.len() {
        assert_eq!(put_chunk_raw(&srv.base, &s, i).status(), 201);
    }
    assert_eq!(finish(&srv.base, &s).status(), 200);

    // 完成后崩溃，重启：对象直接可读；重复 finish 仍幂等
    let srv = srv.restart();
    let got = reqwest::blocking::get(object_url(&srv.base, &s.sha))
        .unwrap()
        .bytes()
        .unwrap();
    assert_eq!(got.as_ref(), s.data.as_slice());

    let r = finish(&srv.base, &s);
    assert_eq!(r.status(), 200);
    assert_eq!(r.json::<Value>().unwrap()["idempotent"], true);
}

#[test]
fn size_mismatch_chunk_rejected() {
    let srv = TestServer::start("sizemis");
    let mut s = make_data(300_000, 128 * 1024);
    create_upload(&srv.base, &mut s);

    // 多发一个字节（声明的 sha 按多发后的内容计算，绕过哈希冲突，专门测长度校验）
    let want = s.chunk_size.min(s.data.len());
    let mut body = s.data[0..want].to_vec();
    body.push(0);
    let declared = sha256_hex(&body);
    let r = reqwest::blocking::Client::new()
        .put(format!(
            "{}/uploads/{}/chunks/0?sha256={}",
            srv.base, s.upload_id, declared
        ))
        .body(body)
        .send()
        .unwrap();
    assert_eq!(r.status(), 400, "oversized chunk must be 400");
}

#[test]
fn bad_requests_are_rejected_early() {
    let srv = TestServer::start("badreq");
    let client = reqwest::blocking::Client::new();

    // 哈希格式错误
    let r = client
        .post(format!("{}/uploads", srv.base))
        .json(&serde_json::json!({"sha256": "zzz", "total_size": 10, "chunk_sizes": [10]}))
        .send()
        .unwrap();
    assert_eq!(r.status(), 400);

    // 长度和不等于总大小
    let r = client
        .post(format!("{}/uploads", srv.base))
        .json(&serde_json::json!({
            "sha256": "a".repeat(64), "total_size": 11, "chunk_sizes": [10]
        }))
        .send()
        .unwrap();
    assert_eq!(r.status(), 400);

    // 创建会话时声明块超过服务上限 -> 413（测试服务器上限 8 MiB）
    let r = client
        .post(format!("{}/uploads", srv.base))
        .json(&serde_json::json!({
            "sha256": "a".repeat(64),
            "total_size": 9 * 1024 * 1024,
            "chunk_sizes": [9 * 1024 * 1024],
        }))
        .send()
        .unwrap();
    assert_eq!(r.status(), 413);

    // 传输体超过服务上限 -> 413（声明块恰好 8 MiB，实传 8 MiB+1）
    let limit = 8 * 1024 * 1024usize;
    let mut edge = make_data(limit, limit);
    create_upload(&srv.base, &mut edge);
    let blob = vec![0xABu8; limit + 1];
    let blob_sha = sha256_hex(&blob);
    let r = client
        .put(format!(
            "{}/uploads/{}/chunks/0?sha256={}",
            srv.base, edge.upload_id, blob_sha
        ))
        .body(blob)
        .send()
        .unwrap();
    assert_eq!(r.status(), 413, "oversized transfer body must be 413");

    // 未知 upload_id
    let r = client
        .put(format!(
            "{}/uploads/00000000-0000-0000-0000-000000000000/chunks/0?sha256={}",
            srv.base,
            "a".repeat(64)
        ))
        .body(b"x".to_vec())
        .send()
        .unwrap();
    assert_eq!(r.status(), 404);

    // 非法 object id
    let r = reqwest::blocking::get(format!("{}/objects/..%2fetc", srv.base)).unwrap();
    assert_eq!(r.status(), 404);
}

#[test]
fn large_multichunk_upload_streaming() {
    // ~9 MiB / 1 MiB = 10 块（最后一块非满），强制 HTTP body 跨多个数据帧
    let srv = TestServer::start("large");
    let mut s = make_data(9 * 1024 * 1024 + 123, 1024 * 1024);
    assert_eq!(s.chunk_sizes.len(), 10);
    create_upload(&srv.base, &mut s);

    // 交错顺序：偶数块先传
    for i in (0..s.chunk_sizes.len()).step_by(2) {
        assert_eq!(put_chunk_raw(&srv.base, &s, i).status(), 201);
    }
    for i in (1..s.chunk_sizes.len()).step_by(2) {
        assert_eq!(put_chunk_raw(&srv.base, &s, i).status(), 201);
    }
    assert_eq!(finish(&srv.base, &s).status(), 200);

    // 流式下载并校验，避免一次性占内存的写法仅用于测试断言
    let resp = reqwest::blocking::get(object_url(&srv.base, &s.sha)).unwrap();
    assert_eq!(resp.content_length(), Some(s.data.len() as u64));
    let got = resp.bytes().unwrap();
    assert_eq!(got.len(), s.data.len());
    assert_eq!(got.as_ref(), s.data.as_slice());
}

#[test]
fn crash_during_finish_restart_allows_recommit() {
    let srv = TestServer::start("crashmid");
    let mut s = make_data(500_000, 128 * 1024);
    create_upload(&srv.base, &mut s);
    for i in 0..s.chunk_sizes.len() {
        assert_eq!(put_chunk_raw(&srv.base, &s, i).status(), 201);
    }

    // 场景 A：finish 在“临时对象已写、rename 前”崩溃
    // -> objects 下残留 .tmp.obj.*，会话仍未提交
    let mut srv2 = srv;
    srv2.kill();
    let objects_dir = srv2.data_dir.join("objects");
    std::fs::write(objects_dir.join(".tmp.obj.deadbeef"), vec![7u8; 12345]).unwrap();
    let data_dir = srv2.data_dir.clone();
    srv2.keep_dir = true;
    drop(srv2);

    let port = free_port();
    let child = Command::new(BIN)
        .env("LISTEN_ADDR", format!("127.0.0.1:{port}"))
        .env("DATA_DIR", &data_dir)
        .env("MAX_CHUNK_SIZE", "8388608")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let srv = TestServer {
        child,
        base: format!("http://127.0.0.1:{port}"),
        data_dir,
        keep_dir: false,
    };
    srv.wait_ready();

    // 残留临时对象应被清理；对象仍不可读；重新 finish 成功
    assert_eq!(
        std::fs::read_dir(&objects_dir).unwrap().count(),
        0,
        "temp object must be cleaned on recovery"
    );
    assert_eq!(
        reqwest::blocking::get(object_url(&srv.base, &s.sha))
            .unwrap()
            .status(),
        404
    );
    assert_eq!(finish(&srv.base, &s).status(), 200);
    assert_eq!(
        reqwest::blocking::get(object_url(&srv.base, &s.sha))
            .unwrap()
            .status(),
        200
    );

    // 场景 B：meta 已标记 committed、对象文件却丢失（极端崩溃窗口）
    // -> 重启时应按分块重新组装发布
    let mut srv2 = srv;
    srv2.kill();
    let obj_path = srv2.data_dir.join("objects").join(&s.sha);
    std::fs::remove_file(&obj_path).unwrap();
    let data_dir = srv2.data_dir.clone();
    srv2.keep_dir = true;
    drop(srv2);

    let port = free_port();
    let child = Command::new(BIN)
        .env("LISTEN_ADDR", format!("127.0.0.1:{port}"))
        .env("DATA_DIR", &data_dir)
        .env("MAX_CHUNK_SIZE", "8388608")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let srv = TestServer {
        child,
        base: format!("http://127.0.0.1:{port}"),
        data_dir,
        keep_dir: false,
    };
    srv.wait_ready();

    let resp = reqwest::blocking::get(object_url(&srv.base, &s.sha)).unwrap();
    assert_eq!(
        resp.status(),
        200,
        "committed object should be re-published on restart"
    );
    assert_eq!(resp.bytes().unwrap().as_ref(), s.data.as_slice());
}
