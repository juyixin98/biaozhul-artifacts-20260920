//! 端到端崩溃恢复测试:以子进程方式启动真实服务,
//! 在每个写入/同步边界注入崩溃,验证恢复语义。

use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn temp_dir(test: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("seglog-test-{test}-{}-{n}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
}

/// 启动服务进程。crash 为 Some 时设置 SEGLOG_CRASH 注入点。
fn start_server(dir: &Path, crash: Option<&str>, max_segment: u64) -> (Child, u16) {
    let port = free_port();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_seglog"));
    cmd.env("SEGLOG_ADDR", format!("127.0.0.1:{port}"))
        .env("SEGLOG_DATA_DIR", dir)
        .env("SEGLOG_MAX_SEGMENT", max_segment.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(c) = crash {
        cmd.env("SEGLOG_CRASH", c);
    }
    let child = cmd.spawn().unwrap();
    (child, port)
}

/// 等待服务就绪;若进程提前退出(如恢复失败拒绝启动)返回 false。
fn wait_ready(child: &mut Child, port: u16) -> bool {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if child.try_wait().unwrap().is_some() {
            return false;
        }
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    false
}

/// 等待进程退出,返回退出状态;超时则杀掉并 panic。
fn wait_exit(child: &mut Child) -> std::process::ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            panic!("进程未在超时时间内退出");
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn kill(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

/// 发送一次 HTTP 请求,返回 (状态码, 响应体)。连接被重置(崩溃)时返回 Err。
fn http(port: u16, method: &str, path: &str, body: &str) -> std::io::Result<(u16, String)> {
    let mut s = TcpStream::connect(("127.0.0.1", port))?;
    s.set_read_timeout(Some(Duration::from_secs(5)))?;
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\
         Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    s.write_all(req.as_bytes())?;
    let mut resp = Vec::new();
    s.read_to_end(&mut resp)?;
    let resp = String::from_utf8_lossy(&resp).into_owned();
    // 服务崩溃时可能读不到任何响应:视为 I/O 错误,由调用方处理。
    let status: u16 = resp
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "空响应或畸形状态行"))?;
    let body_start = resp.find("\r\n\r\n").map(|i| i + 4).unwrap_or(resp.len());
    Ok((status, resp[body_start..].to_string()))
}

fn append(port: u16, log: &str, payload: &str) -> std::io::Result<(u16, String)> {
    http(port, "POST", &format!("/logs/{log}/records"), &format!(r#"{{"payload":"{payload}"}}"#))
}

fn get_record(port: u16, log: &str, seq: u64) -> (u16, String) {
    http(port, "GET", &format!("/logs/{log}/records/{seq}"), "").unwrap()
}

fn list_records(port: u16, log: &str) -> (u16, String) {
    http(port, "GET", &format!("/logs/{log}/records"), "").unwrap()
}

fn seqs_of(body: &str) -> Vec<u64> {
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    v["records"].as_array().unwrap().iter().map(|r| r["seq"].as_u64().unwrap()).collect()
}

fn payload_of(body: &str) -> String {
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    v["payload"].as_str().unwrap().to_string()
}

fn seq_of_append(body: &str) -> u64 {
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    v["seq"].as_u64().unwrap()
}

fn segment_files(dir: &Path, log: &str) -> Vec<PathBuf> {
    let mut files: Vec<_> = fs::read_dir(dir.join(log))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().map(|e| e == "seg").unwrap_or(false))
        .collect();
    files.sort();
    files
}

/// 翻转文件指定偏移处的一个字节(模拟介质损坏)。
fn flip_byte(path: &Path, offset: u64) {
    let mut f = OpenOptions::new().read(true).write(true).open(path).unwrap();
    let mut buf = [0u8; 1];
    f.seek(SeekFrom::Start(offset)).unwrap();
    f.read_exact(&mut buf).unwrap();
    buf[0] ^= 0xFF;
    f.seek(SeekFrom::Start(offset)).unwrap();
    f.write_all(&buf).unwrap();
    f.sync_all().unwrap();
}

// ---------------------------------------------------------------- 基础读写

#[test]
fn basic_append_read_list_status() {
    let dir = temp_dir("basic");
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port), "服务未能启动");

    for (i, p) in ["alpha", "beta", "gamma"].iter().enumerate() {
        let (status, body) = append(port, "events", p).unwrap();
        assert_eq!(status, 201, "追加应返回 201: {body}");
        assert_eq!(seq_of_append(&body), (i + 1) as u64, "序号应从 1 连续分配");
    }

    let (status, body) = get_record(port, "events", 2);
    assert_eq!(status, 200);
    assert_eq!(payload_of(&body), "beta");

    let (status, body) = list_records(port, "events");
    assert_eq!(status, 200);
    assert_eq!(seqs_of(&body), vec![1, 2, 3]);

    let (status, body) = http(port, "GET", "/logs/events/status", "").unwrap();
    assert_eq!(status, 200);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["next_seq"], 4);
    assert_eq!(v["record_count"], 3);

    let (status, _) = get_record(port, "events", 99);
    assert_eq!(status, 404, "不存在的记录应返回 404");

    kill(&mut child);
    fs::remove_dir_all(&dir).ok();
}

// ------------------------------------------------------- 段滚动 + 重启恢复

#[test]
fn segment_roll_and_restart_recovery() {
    let dir = temp_dir("roll");
    // 段上限 128 字节,每条记录约 26 字节,必然跨多个段。
    let (mut child, port) = start_server(&dir, None, 128);
    assert!(wait_ready(&mut child, port));
    for i in 1..=10 {
        let (status, _) = append(port, "events", &format!("rec-{i:02}")).unwrap();
        assert_eq!(status, 201);
    }
    kill(&mut child);

    let segs = segment_files(&dir, "events");
    assert!(segs.len() >= 3, "应滚动出多个段,实际 {:?}", segs);

    // 重启:恢复后所有记录可读,序号连续,新记录接续编号。
    let (mut child, port) = start_server(&dir, None, 128);
    assert!(wait_ready(&mut child, port), "重启恢复失败");
    let (_, body) = list_records(port, "events");
    assert_eq!(seqs_of(&body), (1..=10).collect::<Vec<_>>());
    let (_, body) = get_record(port, "events", 7);
    assert_eq!(payload_of(&body), "rec-07");
    let (status, body) = append(port, "events", "rec-11").unwrap();
    assert_eq!(status, 201);
    assert_eq!(seq_of_append(&body), 11, "恢复后序号不得复用");
    kill(&mut child);
    fs::remove_dir_all(&dir).ok();
}

// ------------------------------------------- 崩溃注入:after_write / after_sync / after_commit

/// 通用崩溃边界测试。crash_point ∈ {after_write, after_sync, after_commit}。
/// 验证:已确认的 r1 必须保留;未确认的 r2 允许存在或丢失;
/// 序号不复用;r3 的序号 = 恢复后最大序号 + 1。
fn crash_boundary(crash_point: &str) {
    let dir = temp_dir(crash_point);

    // 第一次运行:写入已确认记录 r1。
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port));
    let (status, _) = append(port, "events", "r1-confirmed").unwrap();
    assert_eq!(status, 201);
    kill(&mut child); // 正常 kill,r1 已 fsync

    // 第二次运行:注入崩溃,写 r2 时在指定边界崩溃,应答丢失。
    let (mut child, port) = start_server(&dir, Some(crash_point), 1 << 20);
    assert!(wait_ready(&mut child, port));
    let _ = append(port, "events", "r2-unconfirmed"); // 服务崩溃,响应可能拿不到
    let status = wait_exit(&mut child);
    assert_eq!(status.code(), Some(137), "崩溃注入应以 137 退出,实际 {status}");

    // 第三次运行:恢复,验证语义。
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port), "崩溃后恢复失败({crash_point})");

    let (_, body) = get_record(port, "events", 1);
    assert_eq!(payload_of(&body), "r1-confirmed", "已确认记录必须保留({crash_point})");

    let (_, body) = list_records(port, "events");
    let seqs = seqs_of(&body);
    assert!(seqs.first() == Some(&1), "序号必须从 1 连续: {seqs:?}");
    assert!(seqs.windows(2).all(|w| w[1] == w[0] + 1), "序号不得有空洞: {seqs:?}");
    assert!(seqs.len() <= 2, "最多只有 r1 和 r2: {seqs:?}");
    if seqs.len() == 2 {
        // 未确认记录允许存在;若存在,内容必须完整正确(CRC 已校验)。
        let (_, body) = get_record(port, "events", 2);
        assert_eq!(payload_of(&body), "r2-unconfirmed");
    }

    // 新记录序号 = 最大序号 + 1,绝不复用。
    let (status, body) = append(port, "events", "r3-after-crash").unwrap();
    assert_eq!(status, 201);
    assert_eq!(seq_of_append(&body), seqs.len() as u64 + 1, "序号不得复用({crash_point})");
    kill(&mut child);
    fs::remove_dir_all(&dir).ok();
}

#[test]
fn crash_after_write_boundary() {
    crash_boundary("after_write");
}

#[test]
fn crash_after_sync_boundary() {
    crash_boundary("after_sync");
}

#[test]
fn crash_after_commit_boundary() {
    crash_boundary("after_commit");
}

/// 反复崩溃-恢复循环,并使用小段强制跨段滚动:
/// 每轮先注入 after_commit 崩溃(未确认记录),再正常写入一条已确认记录。
#[test]
fn repeated_crash_recovery_loop() {
    let dir = temp_dir("loop");
    let mut confirmed: Vec<String> = Vec::new();

    for round in 0..6 {
        // 注入崩溃:第一条 append 在 after_commit 处崩溃(已持久化但应答丢失)。
        let (mut child, port) = start_server(&dir, Some("after_commit"), 200);
        assert!(wait_ready(&mut child, port), "第 {round} 轮: 注入服务启动失败");
        let _ = append(port, "events", &format!("unconfirmed-{round}"));
        wait_exit(&mut child);

        // 正常重启:恢复并验证,然后写一条已确认记录。
        let (mut child, port) = start_server(&dir, None, 200);
        assert!(wait_ready(&mut child, port), "第 {round} 轮: 恢复失败");

        let (_, body) = list_records(port, "events");
        let seqs = seqs_of(&body);
        let expected: Vec<u64> = (1..=seqs.len() as u64).collect();
        assert_eq!(seqs, expected, "第 {round} 轮: 序号必须从 1 连续");

        // 所有已确认记录必须原样保留(按追加时返回的 seq 逐一核对)。
        for item in &confirmed {
            let (seq, payload) = item.split_once(':').unwrap();
            let (status, body) = get_record(port, "events", seq.parse().unwrap());
            assert_eq!(status, 200, "第 {round} 轮: 已确认记录 seq={seq} 丢失");
            assert_eq!(payload_of(&body), payload, "第 {round} 轮: seq={seq} 内容被篡改");
        }
        let payload = format!("confirmed-{round}");
        let (status, body) = append(port, "events", &payload).unwrap();
        assert_eq!(status, 201);
        let new_seq = seq_of_append(&body);
        assert_eq!(new_seq, seqs.len() as u64 + 1, "第 {round} 轮: 序号不得复用");
        confirmed.push(format!("{new_seq}:{payload}"));
        kill(&mut child);
    }

    // 最终恢复:逐条核对所有已确认记录。
    let (mut child, port) = start_server(&dir, None, 200);
    assert!(wait_ready(&mut child, port), "最终恢复失败");
    for item in &confirmed {
        let (seq, payload) = item.split_once(':').unwrap();
        let (status, body) = get_record(port, "events", seq.parse().unwrap());
        assert_eq!(status, 200, "已确认记录 seq={seq} 丢失");
        assert_eq!(payload_of(&body), payload, "已确认记录 seq={seq} 内容被篡改");
    }
    let (_, body) = list_records(port, "events");
    let seqs = seqs_of(&body);
    let expected: Vec<u64> = (1..=seqs.len() as u64).collect();
    assert_eq!(seqs, expected, "最终序号必须连续");
    assert!(segment_files(&dir, "events").len() >= 2, "应发生段滚动");
    kill(&mut child);
    fs::remove_dir_all(&dir).ok();
}

// ------------------------------------------------------- 尾部截断

#[test]
fn incomplete_tail_record_truncated() {
    let dir = temp_dir("tail");
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port));
    append(port, "events", "rec-1").unwrap();
    append(port, "events", "rec-2").unwrap();
    kill(&mut child);

    // 模拟撕裂写:追加一个头部完整但记录体不完整的尾记录。
    let seg = segment_files(&dir, "events").pop().unwrap();
    let mut f = OpenOptions::new().append(true).open(&seg).unwrap();
    let mut torn = seglog::record::encode(3, b"rec-3-torn-write-payload");
    torn.truncate(torn.len() - 7); // 截掉部分 payload
    f.write_all(&torn).unwrap();
    f.sync_all().unwrap();
    drop(f);

    // 重启:不完整尾记录被截去,前两条保留,新记录序号为 3(不复用也不空洞)。
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port), "截断恢复失败");
    let (_, body) = list_records(port, "events");
    assert_eq!(seqs_of(&body), vec![1, 2]);
    let (status, body) = append(port, "events", "rec-3").unwrap();
    assert_eq!(status, 201);
    assert_eq!(seq_of_append(&body), 3);
    let (_, body) = get_record(port, "events", 3);
    assert_eq!(payload_of(&body), "rec-3");
    kill(&mut child);
    fs::remove_dir_all(&dir).ok();
}

// ------------------------------------------- 损坏必须拒绝打开,不得静默跳过

#[test]
fn corrupt_middle_segment_rejected() {
    let dir = temp_dir("midcorrupt");
    let (mut child, port) = start_server(&dir, None, 128);
    assert!(wait_ready(&mut child, port));
    for i in 1..=10 {
        append(port, "events", &format!("rec-{i:02}")).unwrap();
    }
    kill(&mut child);

    let segs = segment_files(&dir, "events");
    assert!(segs.len() >= 2, "需要至少两个段");
    // 损坏第一个段(封存的中段)中间的一个字节。
    flip_byte(&segs[0], 25);

    // 重启必须失败(拒绝打开),而不是静默跳过。
    let (mut child, port) = start_server(&dir, None, 128);
    assert!(!wait_ready(&mut child, port), "中段损坏时服务不应就绪");
    let status = wait_exit(&mut child);
    assert!(!status.success(), "中段损坏必须以非零码退出,实际 {status}");
    fs::remove_dir_all(&dir).ok();
}

#[test]
fn corrupt_tail_record_crc_rejected() {
    let dir = temp_dir("tailcrc");
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port));
    append(port, "events", "rec-1").unwrap();
    append(port, "events", "rec-2").unwrap();
    kill(&mut child);

    // 损坏末段最后一条完整记录的最后一个 payload 字节(CRC 必然不匹配)。
    let seg = segment_files(&dir, "events").pop().unwrap();
    let len = fs::metadata(&seg).unwrap().len();
    flip_byte(&seg, len - 1);

    // 完整但 CRC 错误的尾记录不属于"不完整尾记录",必须拒绝打开。
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(!wait_ready(&mut child, port), "CRC 损坏时服务不应就绪");
    let status = wait_exit(&mut child);
    assert!(!status.success(), "CRC 损坏必须以非零码退出,实际 {status}");
    fs::remove_dir_all(&dir).ok();
}

#[test]
fn corrupt_magic_rejected() {
    let dir = temp_dir("magic");
    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(wait_ready(&mut child, port));
    append(port, "events", "rec-1").unwrap();
    kill(&mut child);

    // 破坏第一条记录的魔数。
    let seg = segment_files(&dir, "events").pop().unwrap();
    flip_byte(&seg, 0);

    let (mut child, port) = start_server(&dir, None, 1 << 20);
    assert!(!wait_ready(&mut child, port), "魔数损坏时服务不应就绪");
    let status = wait_exit(&mut child);
    assert!(!status.success());
    fs::remove_dir_all(&dir).ok();
}
