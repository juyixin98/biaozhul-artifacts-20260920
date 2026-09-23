//! 验收测试：
//! 1. 跨三层同键覆盖后删除：墓碑必须保留到确认更老层无同键，旧值永不复活。
//! 2. 合并中断（进程内 panic）：manifest 未切换，可视图不变，孤儿段在重开时清理。
//! 3. 真实子进程 exit(42) 崩溃 + 重启：HTTP 端到端验证。
//! 4. 范围扫描跨层去重、排序、墓碑不输出。

use lsm_server::engine::{Engine, Fault};
use std::collections::HashSet;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

fn temp_dir(tag: &str) -> PathBuf {
    static N: AtomicU64 = AtomicU64::new(0);
    let n = N.fetch_add(1, Ordering::SeqCst);
    let d = std::env::temp_dir().join(format!(
        "lsm-test-{}-{}-{n}",
        std::process::id(),
        tag
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// 构造跨三层同键覆盖：L2=v1, L1=v2, L0=v3。
///
/// flush 永远产出 L0；用逐级合并把老版本“推”上 L2：
///   v1: flush -> L0, compact0 -> L1, compact1 -> L2
///   v2: flush -> L0, compact0 -> L1
///   v3: flush -> L0
fn build_three_layers(e: &Engine) {
    e.put("k".into(), "v1".into()).unwrap();
    e.flush().unwrap();
    e.compact(0, Fault::None).unwrap();
    e.compact(1, Fault::None).unwrap();

    e.put("k".into(), "v2".into()).unwrap();
    e.flush().unwrap();
    e.compact(0, Fault::None).unwrap();

    e.put("k".into(), "v3".into()).unwrap();
    e.flush().unwrap();
}

#[test]
fn basic_put_get_delete_and_persistence() {
    let dir = temp_dir("basic");
    let e = Engine::open(&dir, 1000).unwrap();
    let s1 = e.put("a".into(), "1".into()).unwrap();
    let s2 = e.put("a".into(), "2".into()).unwrap();
    assert!(s2 > s1);
    assert_eq!(e.get("a").unwrap().value, "2");
    e.delete("a".into()).unwrap();
    assert!(e.get("a").is_none(), "tombstone must hide the key");
    e.flush().unwrap();
    assert!(e.get("a").is_none(), "tombstone still hides after flush");

    // 重开：memtable 已清空，数据全部来自段文件。
    drop(e);
    let e2 = Engine::open(&dir, 1000).unwrap();
    assert!(e2.get("a").is_none());
    e2.put("b".into(), "hello".into()).unwrap();
    e2.flush().unwrap();
    drop(e2);
    let e3 = Engine::open(&dir, 1000).unwrap();
    assert_eq!(e3.get("b").unwrap().value, "hello");
}

#[test]
fn three_layer_overwrite_then_delete_tombstone_lifecycle() {
    let dir = temp_dir("layers");
    let e = Engine::open(&dir, 10000).unwrap();
    build_three_layers(&e);

    let st = e.state();
    let levels: HashSet<usize> = st.segments.iter().map(|s| s.level).collect();
    assert!(levels.contains(&0) && levels.contains(&1) && levels.contains(&2),
        "expected segments on L0/L1/L2, got levels {levels:?}");
    assert_eq!(e.get("k").unwrap().value, "v3");

    // 删除并刷盘：墓碑落在 L0。
    e.delete("k".into()).unwrap();
    e.flush().unwrap();
    assert!(e.get("k").is_none());

    // 合并 L0+L1：墓碑胜出，但 L2 仍有同键 -> 必须保留墓碑。
    let r0 = e.compact(0, Fault::None).unwrap();
    assert_eq!(r0.tombstones_kept, 1, "tombstone must be kept while L2 holds the key");
    assert_eq!(r0.tombstones_dropped, 0);
    assert!(e.get("k").is_none(), "old value must not resurrect after compact 0");

    // 合并 L1+L2：仍无更老层之外的键……此时更老层（>2）为空，墓碑方可丢弃。
    let r1 = e.compact(1, Fault::None).unwrap();
    assert_eq!(r1.tombstones_dropped, 1, "tombstone can be dropped only now");
    assert_eq!(r1.tombstones_kept, 0);
    assert!(e.get("k").is_none(), "old value must not resurrect after tombstone dropped");

    // 段中也不应再残留 k：range 全空即证明无任何版本可见。
    assert!(e.range(None, None, None).is_empty());
}

#[test]
fn compaction_panic_leaves_published_view_intact() {
    let dir = temp_dir("panic");
    let e = Engine::open(&dir, 10000).unwrap();
    build_three_layers(&e);
    e.delete("k".into()).unwrap();
    e.flush().unwrap();

    let before = e.state();
    let seg_files_before: HashSet<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|x| x.unwrap().file_name().to_string_lossy().to_string())
        .collect();

    // 新段文件写完、manifest 切换前 panic（模拟掉电）。
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        e.compact(0, Fault::PanicAfterSegWrite).unwrap();
    }));
    assert!(result.is_err(), "fault injection must panic");

    // 进程未死：已发布的清单不变，可视图不变。
    let after = e.state();
    assert_eq!(before.segments.len(), after.segments.len());
    assert_eq!(
        before.segments.iter().map(|s| s.id).collect::<HashSet<_>>(),
        after.segments.iter().map(|s| s.id).collect::<HashSet<_>>()
    );
    assert!(e.get("k").is_none(), "tombstone view must survive the crash");

    // 新段文件确实落盘了（只是没登记进 manifest）。
    let seg_files_after: HashSet<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|x| x.unwrap().file_name().to_string_lossy().to_string())
        .collect();
    let orphan: Vec<_> = seg_files_after.difference(&seg_files_before).cloned().collect();
    assert!(
        orphan.iter().any(|n| n.starts_with("seg-") && n.ends_with(".jsonl")),
        "expected an orphan segment file, got {orphan:?}"
    );

    // 重开引擎：孤儿被清理，manifest 仍指向旧段集合。
    drop(e);
    let reopened = Engine::open(&dir, 10000).unwrap();
    assert!(reopened.get("k").is_none());
    let files: HashSet<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|x| x.unwrap().file_name().to_string_lossy().to_string())
        .collect();
    for o in &orphan {
        assert!(!files.contains(o), "orphan {o} must be cleaned on startup");
    }

    // 中断恢复后继续完成合并链路，旧值依旧不复活。
    let r0 = reopened.compact(0, Fault::None).unwrap();
    assert_eq!(r0.tombstones_kept, 1);
    assert!(reopened.get("k").is_none());
    let r1 = reopened.compact(1, Fault::None).unwrap();
    assert_eq!(r1.tombstones_dropped, 1);
    assert!(reopened.get("k").is_none());
}

#[test]
fn range_scan_deduplicates_and_hides_tombstones() {
    let dir = temp_dir("range");
    let e = Engine::open(&dir, 10000).unwrap();

    // 五个键，每个键多次覆盖并散落到不同层；c 删除。
    for (k, v) in [("a", "a1"), ("b", "b1"), ("c", "c1"), ("d", "d1"), ("e", "e1")] {
        e.put(k.into(), v.into()).unwrap();
    }
    e.flush().unwrap();
    e.compact(0, Fault::None).unwrap();
    e.compact(1, Fault::None).unwrap(); // 全部推到 L2

    for (k, v) in [("a", "a2"), ("b", "b2"), ("c", "c2"), ("d", "d2")] {
        e.put(k.into(), v.into()).unwrap();
    }
    e.flush().unwrap();
    e.compact(0, Fault::None).unwrap(); // 新版本在 L1

    e.put("a".into(), "a3".into()).unwrap();
    e.put("c".into(), "c3".into()).unwrap();
    e.flush().unwrap(); // L0
    e.delete("c".into()).unwrap(); // 墓碑先在 memtable

    let all = e.range(None, None, None);
    let keys: Vec<_> = all.iter().map(|kv| kv.key.clone()).collect();
    assert_eq!(keys, vec!["a", "b", "d", "e"], "c deleted, no dup, sorted");
    let unique: HashSet<_> = keys.iter().collect();
    assert_eq!(unique.len(), keys.len(), "range must not emit duplicate keys");
    assert_eq!(all.iter().find(|kv| kv.key == "a").unwrap().value, "a3");
    assert_eq!(all.iter().find(|kv| kv.key == "e").unwrap().value, "e1");

    // 半开区间 + limit。
    let mid = e.range(Some("b"), Some("e"), None);
    assert_eq!(mid.iter().map(|kv| kv.key.clone()).collect::<Vec<_>>(), vec!["b", "d"]);
    let limited = e.range(None, None, Some(2));
    assert_eq!(limited.len(), 2);

    // 刷盘后墓碑同样不输出。
    e.flush().unwrap();
    e.compact(0, Fault::None).unwrap(); // 墓碑因更老层存在而保留
    let all2 = e.range(None, None, None);
    assert_eq!(all2.iter().map(|kv| kv.key.clone()).collect::<Vec<_>>(), vec!["a", "b", "d", "e"]);
}

#[test]
fn auto_flush_triggers_at_threshold() {
    let dir = temp_dir("auto");
    let e = Engine::open(&dir, 3).unwrap();
    for i in 0..7 {
        e.put(format!("key{i}"), format!("v{i}")).unwrap();
    }
    let st = e.state();
    assert!(st.memtable_len < 3, "memtable should have been flushed: {st:?}");
    assert!(!st.segments.is_empty());
    for i in 0..7 {
        assert_eq!(e.get(&format!("key{i}")).unwrap().value, format!("v{i}"));
    }
}

// ---------- 真实子进程崩溃 + 重启的端到端测试 ----------

struct Server {
    child: std::process::Child,
    base: String,
    _dir: PathBuf,
    log: PathBuf,
}

fn free_port() -> u16 {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    l.local_addr().unwrap().port()
}

fn http(base: &str, method: &str, path: &str, body: &str) -> (u16, String) {
    let addr = base.strip_prefix("http://").unwrap();
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).unwrap();
    let mut raw = Vec::new();
    let _ = stream.read_to_end(&mut raw);
    let raw = String::from_utf8_lossy(&raw);
    let (head, body_s) = raw.split_once("\r\n\r\n").unwrap_or((&raw, ""));
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    // 简单处理 chunked 响应。
    let lower = head.to_ascii_lowercase();
    let parsed = if lower.contains("transfer-encoding: chunked") {
        dechunk(body_s)
    } else {
        body_s.to_string()
    };
    (status, parsed.trim().to_string())
}

fn dechunk(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let line_end = bytes[i..].windows(2).position(|w| w == b"\r\n").map(|p| i + p);
        let Some(line_end) = line_end else { break };
        let size_str = std::str::from_utf8(&bytes[i..line_end]).unwrap();
        let size = usize::from_str_radix(size_str.trim(), 16).unwrap_or(0);
        if size == 0 {
            break;
        }
        let start = line_end + 2;
        out.extend_from_slice(&bytes[start..start + size]);
        i = start + size + 2;
    }
    String::from_utf8(out).unwrap()
}

fn wait_ready(base: &str) {
    for _ in 0..100 {
        if std::net::TcpStream::connect(base.strip_prefix("http://").unwrap()).is_ok() {
            // 再确认服务真的能响应。
            if http(base, "GET", "/admin/state", "").0 == 200 {
                return;
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("server at {base} did not become ready");
}

fn start_server(dir: &PathBuf, tag: &str) -> Server {
    let port = free_port();
    let log = dir.join(format!("server-{tag}.log"));
    let logf = std::fs::File::create(&log).unwrap();
    let child = std::process::Command::new(env!("CARGO_BIN_EXE_lsm-server"))
        .args([
            "--addr", format!("127.0.0.1:{port}").as_str(),
            "--data-dir", dir.to_str().unwrap(),
            "--max-mem", "10000",
        ])
        .stdout(logf.try_clone().unwrap())
        .stderr(logf)
        .spawn()
        .unwrap();
    let base = format!("http://127.0.0.1:{port}");
    wait_ready(&base);
    Server { child, base, _dir: dir.clone(), log }
}

fn put(base: &str, k: &str, v: &str) {
    let (code, body) = http(base, "PUT", &format!("/kv/{k}"), &format!(r#"{{"value":"{v}"}}"#));
    assert_eq!(code, 200, "PUT {k} failed: {body}");
}

#[test]
fn end_to_end_crash_during_compaction_and_restart() {
    let dir = temp_dir("e2e");
    let bin = env!("CARGO_BIN_EXE_lsm-server");
    assert!(std::path::Path::new(bin).exists());

    // --- 第一次启动：构造跨三层同键覆盖 ---
    let mut srv = start_server(&dir, "1");
    let base = srv.base.clone();

    put(&base, "k", "v1");
    assert_eq!(http(&base, "POST", "/admin/flush", "").0, 200);
    assert_eq!(http(&base, "POST", "/admin/compact?level=0", "").0, 200);
    assert_eq!(http(&base, "POST", "/admin/compact?level=1", "").0, 200);

    put(&base, "k", "v2");
    assert_eq!(http(&base, "POST", "/admin/flush", "").0, 200);
    assert_eq!(http(&base, "POST", "/admin/compact?level=0", "").0, 200);

    put(&base, "k", "v3");
    for (k, v) in [("a", "a1"), ("m", "m1"), ("z", "z1")] {
        put(&base, k, v);
    }
    assert_eq!(http(&base, "POST", "/admin/flush", "").0, 200);

    let (code, body) = http(&base, "GET", "/kv/k", "");
    assert_eq!(code, 200);
    assert!(body.contains("v3"), "expected v3 visible, got {body}");

    // 删除 k 并刷盘。
    assert_eq!(http(&base, "DELETE", "/kv/k", "").0, 200);
    assert_eq!(http(&base, "POST", "/admin/flush", "").0, 200);
    assert_eq!(http(&base, "GET", "/kv/k", "").0, 404);

    // --- 在合并 L0+L1 时注入真实进程退出（段已落盘、清单未切换）---
    let _ = http(&base, "POST", "/admin/compact?level=0&fault=exit_after_write", "");
    let status = srv.child.wait().unwrap();
    assert_eq!(status.code(), Some(42), "expected crash exit code 42; see {}", srv.log.display());

    // 磁盘现场：存在未登记的孤儿段文件。
    let files: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|x| x.unwrap().file_name().to_string_lossy().to_string())
        .collect();
    assert!(files.iter().any(|f| f.starts_with("seg-") && f.ends_with(".jsonl")));
    let manifest_before = std::fs::read_to_string(dir.join("manifest.json")).unwrap();

    // --- 第二次启动：恢复 ---
    let mut srv2 = start_server(&dir, "2");
    let base2 = srv2.base.clone();

    // 旧值不复活。
    assert_eq!(http(&base2, "GET", "/kv/k", "").0, 404, "old value must not resurrect after restart");

    // 范围扫描无重复、无 k、有序。
    let (code, body) = http(&base2, "GET", "/range", "");
    assert_eq!(code, 200);
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    let keys: Vec<String> = v["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| i["key"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(keys, vec!["a", "m", "z"], "range after restart: {body}");
    let uniq: HashSet<&String> = keys.iter().collect();
    assert_eq!(uniq.len(), keys.len(), "duplicate keys in range scan");

    // 清单在恢复后没有被那次失败的合并改变（段集合一致）。
    let st: serde_json::Value =
        serde_json::from_str(&http(&base2, "GET", "/admin/state", "").1).unwrap();
    let ids: HashSet<u64> = st["segments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["id"].as_u64().unwrap())
        .collect();
    let mf: serde_json::Value = serde_json::from_str(&manifest_before).unwrap();
    let mf_ids: HashSet<u64> = mf["segments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["id"].as_u64().unwrap())
        .collect();
    assert_eq!(ids, mf_ids, "manifest must be unchanged by the crashed compaction");

    // --- 恢复后继续合并：墓碑先保留、再安全丢弃，全程旧值不复活 ---
    let (_, body) = http(&base2, "POST", "/admin/compact?level=0", "");
    let r: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(r["tombstones_kept"].as_u64(), Some(1));
    assert_eq!(http(&base2, "GET", "/kv/k", "").0, 404);

    let (_, body) = http(&base2, "POST", "/admin/compact?level=1", "");
    let r: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(r["tombstones_dropped"].as_u64(), Some(1));
    assert_eq!(http(&base2, "GET", "/kv/k", "").0, 404);

    let body = http(&base2, "GET", "/range", "").1;
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    let keys: Vec<String> = v["items"].as_array().unwrap().iter()
        .map(|i| i["key"].as_str().unwrap().to_string()).collect();
    assert_eq!(keys, vec!["a", "m", "z"]);

    // --- 第三次启动：最终状态持久化正确 ---
    srv2.child.kill().unwrap();
    srv2.child.wait().unwrap();
    let mut srv3 = start_server(&dir, "3");
    assert_eq!(http(&srv3.base, "GET", "/kv/k", "").0, 404);
    let body = http(&srv3.base, "GET", "/range", "").1;
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["count"].as_u64(), Some(3));

    srv3.child.kill().unwrap();
    srv3.child.wait().unwrap();
}
